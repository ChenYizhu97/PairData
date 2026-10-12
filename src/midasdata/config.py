"""TOML plus CLI overrides; .env is read as data, never evaluated as shell code."""

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
    pass


class ModelConfig(Contract):
    model: Text = "unset"
    base_url: Text = "https://api.openai.com/v1"
    api_key_env: Text = "OPENAI_API_KEY"
    structured_output: Literal["native", "json"] = "json"
    temperature: float = 0.0
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    extra_body: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_url(self):
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("base_url must be HTTP(S), without embedded credentials")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must be an environment variable name")
        # DeepSeek has an explicit thinking switch. Apply its default to both
        # model roles, preserving intentional extra_body overrides.
        host = url.hostname or ""
        if host == "deepseek.com" or host.endswith(".deepseek.com"):
            self.extra_body = {"thinking": {"type": "disabled"}, **self.extra_body}
        try:
            json.dumps(self.extra_body, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("extra_body must contain JSON-compatible values") from exc
        return self


class PIURange(Contract):
    min: int = Field(ge=0)
    max: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_range(self):
        if self.min > self.max:
            raise ValueError("PIU min must not exceed max")
        return self


class PIUConfig(Contract):
    light: PIURange = Field(default_factory=lambda: PIURange(min=0, max=0))
    medium: PIURange = Field(default_factory=lambda: PIURange(min=2, max=4))
    rich: PIURange = Field(default_factory=lambda: PIURange(min=5, max=8))

    @model_validator(mode="after")
    def validate_order(self):
        if self.light.min != 0 or self.light.max != 0:
            raise ValueError("Light must preserve the original with 0 added PIUs")
        if not self.light.max < self.medium.min <= self.medium.max < self.rich.min:
            raise ValueError("PIU ranges must increase: light < medium < rich")
        return self


class Config(Contract):
    _api_keys: dict[str, str | None] = PrivateAttr(default_factory=dict)

    generator: ModelConfig = Field(default_factory=ModelConfig)
    validator: ModelConfig = Field(default_factory=ModelConfig)
    concurrency: int = Field(default=4, ge=1, le=256)
    timeout: float = Field(default=120.0, gt=0)
    preview_page_size: int = Field(default=100, ge=1)
    retry_initial: float = Field(default=1.0, ge=0)
    retry_max: float = Field(default=30.0, ge=0)
    piu: PIUConfig = Field(default_factory=PIUConfig)

    def api_key(self, settings: ModelConfig) -> str | None:
        if settings.api_key_env in self._api_keys:
            return self._api_keys[settings.api_key_env]
        return os.environ.get(settings.api_key_env)

    @model_validator(mode="after")
    def validate_retry(self):
        if self.retry_max < self.retry_initial:
            raise ValueError("retry_max must be >= retry_initial")
        return self


ENV_ALIASES = {
    "OPENAI_BASE_URL": ("base_url", "BASE_URL"),
    "OPENAI_API_KEY": ("api_key", "API_KEY"),
    "MODEL_NAME": ("model", "MODEL"),
}


def _read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip().removeprefix("export ")
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ConfigurationError(f"{path}:{number}: invalid environment assignment")
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ConfigurationError(f"{path}:{number}: unclosed quote")
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            values.setdefault(key, value)
    return values


def _normalize_aliases(values: dict[str, str]) -> dict[str, str]:
    normalized = dict(values)
    for canonical, aliases in ENV_ALIASES.items():
        for alias in aliases:
            if alias in values:
                normalized.setdefault(canonical, values[alias])
    return normalized


def load_env(path: Path) -> dict[str, str]:
    # Normalize each source before merging so process aliases also override .env.
    process_values = _normalize_aliases(dict(os.environ))
    file_values = _normalize_aliases(_read_env(path))
    return {**file_values, **process_values}


def _interpolate(value, environment: dict[str, str]):
    if isinstance(value, dict):
        return {key: _interpolate(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate(item, environment) for item in value]
    if isinstance(value, str):

        def replace(match):
            name = match[1]
            if name not in environment:
                raise ConfigurationError(f"environment variable {name} is missing")
            return environment[name]

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    return value


def load_config(path: Path | None, overrides: dict, env_file: Path = Path(".env")) -> Config:
    environment = load_env(env_file)
    defaults = {
        "model": environment.get("MODEL_NAME", "unset"),
        "base_url": environment.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key_env": "OPENAI_API_KEY",
    }
    data = {"generator": {**defaults, "temperature": 0.8}, "validator": dict(defaults)}
    if path:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
        for key, value in loaded.items():
            if key in {"generator", "validator"}:
                if not isinstance(value, dict):
                    raise ConfigurationError(f"{key} must be a TOML table")
                data[key].update(value)
            else:
                data[key] = value
    for key, value in overrides.items():
        if value is None:
            continue
        if key in {"model", "validator_model", "base_url", "structured_output"}:
            if key == "model":
                targets = ("generator",)
            elif key == "validator_model":
                targets = ("validator",)
            else:
                targets = ("generator", "validator")
            field = "model" if key in {"model", "validator_model"} else key
            for target in targets:
                data[target][field] = value
        else:
            data[key] = value
    config = Config.model_validate(_interpolate(data, environment))
    config._api_keys = {
        settings.api_key_env: environment.get(settings.api_key_env)
        for settings in (config.generator, config.validator)
    }
    return config


def check_credentials(config: Config) -> None:
    for name in ("generator", "validator"):
        model = getattr(config, name)
        if model.model == "unset":
            raise ConfigurationError(f"{name}.model / MODEL_NAME is required")
        if not config.api_key(model):
            raise ConfigurationError(f"missing API key environment variable: {model.api_key_env}")
