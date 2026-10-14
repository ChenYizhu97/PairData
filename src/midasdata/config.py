"""配置加载：进程环境 > .env；CLI > TOML > 环境默认值。

Configuration precedence: process environment > .env; CLI > TOML > environment defaults.
"""

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, PrivateAttr, model_validator

from midasdata.schemas import Contract, Text


class ConfigurationError(Exception):
    """需要修正配置后才能继续运行的错误。

    An error that requires a configuration fix before the run can continue.
    """


class ModelSettings(Contract):
    model: Text = "unset"
    base_url: Text = "https://api.openai.com/v1"
    api_key_env: Text = "OPENAI_API_KEY"
    structured_output: Literal["json", "native"] = "json"
    temperature: float = 0.0
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    extra_body: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_provider(self):
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("base_url must be HTTP(S), without credentials, query or fragment")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must name an environment variable")
        try:
            json.dumps(self.extra_body, allow_nan=False)
        except (ValueError, TypeError) as error:
            raise ValueError("extra_body must contain JSON-compatible values") from error
        if url.hostname == "deepseek.com" or (url.hostname or "").endswith(".deepseek.com"):
            self.extra_body = {"thinking": {"type": "disabled"}, **self.extra_body}
        return self


class AdditionRange(Contract):
    min: int = Field(ge=0)
    max: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.min > self.max:
            raise ValueError("PIU min must not exceed max")
        return self


class ExpansionRanges(Contract):
    light: AdditionRange = Field(default_factory=lambda: AdditionRange(min=0, max=0))
    medium: AdditionRange = Field(default_factory=lambda: AdditionRange(min=2, max=4))
    rich: AdditionRange = Field(default_factory=lambda: AdditionRange(min=5, max=8))

    @model_validator(mode="after")
    def validate_levels(self):
        if self.light.min != 0 or self.light.max != 0:
            raise ValueError("Light requires exactly zero added PIUs")
        if not self.light.max < self.medium.min <= self.medium.max < self.rich.min:
            raise ValueError("PIU ranges must increase without overlap")
        return self


class RunSettings(Contract):
    generator: ModelSettings = Field(default_factory=ModelSettings)
    validator: ModelSettings = Field(default_factory=ModelSettings)
    concurrency: int = Field(default=4, ge=1, le=256)
    timeout: float = Field(default=120.0, gt=0)
    preview_page_size: int = Field(default=100, ge=1)
    retry_initial: float = Field(default=1.0, ge=0)
    retry_max: float = Field(default=30.0, ge=0)
    piu: ExpansionRanges = Field(default_factory=ExpansionRanges)
    _credentials: dict[str, str | None] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def validate_backoff(self):
        if self.retry_max < self.retry_initial:
            raise ValueError("retry_max must be >= retry_initial")
        return self

    def api_key(self, model: ModelSettings) -> str | None:
        return self._credentials.get(model.api_key_env)

    def require_credentials(self) -> None:
        for role in ("generator", "validator"):
            model = getattr(self, role)
            if model.model == "unset":
                raise ConfigurationError(f"{role}.model / MODEL_NAME is required")
            if not self.api_key(model):
                raise ConfigurationError(
                    f"missing API key environment variable: {model.api_key_env}"
                )


def read_env_file(path: Path) -> dict[str, str]:
    """只解析文本，不执行 shell 表达式，也不修改 os.environ。

    Parse text without executing shell expressions or modifying os.environ.
    """
    values = {}
    if not path.exists():
        return values
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip().removeprefix("export ")
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigurationError(f"{path}:{line_number}: invalid environment assignment")
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigurationError(f"{path}:{line_number}: unclosed quote")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values.setdefault(key, value)
    return values


def interpolate_environment(value, environment: dict[str, str]):
    if isinstance(value, dict):
        return {key: interpolate_environment(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate_environment(item, environment) for item in value]
    if not isinstance(value, str):
        return value

    def substitute(match):
        name = match[1]
        if name not in environment:
            raise ConfigurationError(f"environment variable {name} is missing")
        return environment[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, value)


def load_settings(
    config_file: Path | None,
    overrides: dict,
    env_file: Path = Path(".env"),
) -> RunSettings:
    environment = {**read_env_file(env_file), **os.environ}
    model_defaults = {
        "model": environment.get("MODEL_NAME", "unset"),
        "base_url": environment.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key_env": "OPENAI_API_KEY",
    }
    values = {
        "generator": {**model_defaults, "temperature": 0.8},
        "validator": {**model_defaults, "temperature": 0.0},
    }
    if config_file:
        with config_file.open("rb") as handle:
            configured = tomllib.load(handle)
        for key, value in configured.items():
            if key in {"generator", "validator"}:
                if not isinstance(value, dict):
                    raise ConfigurationError(f"{key} must be a TOML table")
                values[key].update(value)
            else:
                values[key] = value
    model_overrides = {
        "model": ("model", ("generator",)),
        "validator_model": ("model", ("validator",)),
        "base_url": ("base_url", ("generator", "validator")),
        "structured_output": ("structured_output", ("generator", "validator")),
    }
    for key, value in overrides.items():
        if value is None:
            continue
        if key in model_overrides:
            field, roles = model_overrides[key]
            for role in roles:
                values[role][field] = value
        else:
            values[key] = value
    settings = RunSettings.model_validate(interpolate_environment(values, environment))
    settings._credentials = {
        model.api_key_env: environment.get(model.api_key_env)
        for model in (settings.generator, settings.validator)
    }
    return settings
