"""Fixed YAML configuration contracts; business responses use JSON Schema."""

import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, JsonValue, PrivateAttr, model_validator

from pairdata.bindings import validate_binding
from pairdata.schemas import Contract, Text


class ConfigurationError(Exception):
    """A configuration error that requires stopping the run."""


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
        return self


class RunSettings(Contract):
    concurrency: int = Field(default=4, ge=1, le=256)
    timeout: float = Field(default=120.0, gt=0)
    preview_page_size: int = Field(default=100, ge=1)
    retry_initial: float = Field(default=1.0, ge=0)
    retry_max: float = Field(default=30.0, ge=0)
    max_request_attempts: int = Field(default=5, ge=1, le=100)

    @model_validator(mode="after")
    def check_backoff(self):
        if self.retry_max < self.retry_initial:
            raise ValueError("retry_max must be >= retry_initial")
        return self


class MessageSettings(Contract):
    role: Literal["system", "user"]
    template: Text


class CallSettings(Contract):
    model: Text
    inputs: dict[str, JsonValue]
    messages: list[MessageSettings] = Field(min_length=1)
    response_schema: Text


class GenerateSettings(CallSettings):
    output_path: Text


class StageSettings(Contract):
    sampler: Text
    generate: GenerateSettings
    evaluate: CallSettings
    decision: Text
    max_generations: int = Field(default=3, ge=1, le=100)


class RecipeSettings(Contract):
    name: Text
    models: dict[str, ModelSettings] = Field(min_length=1)
    runtime: RunSettings = Field(default_factory=RunSettings)
    source: StageSettings
    target: StageSettings
    _credentials: dict[str, str | None] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def check_calls(self):
        for stage_name in ("source", "target"):
            stage = getattr(self, stage_name)
            for operation in ("generate", "evaluate"):
                call = getattr(stage, operation)
                if call.model not in self.models:
                    raise ValueError(f"{stage_name}.{operation}: unknown model {call.model}")
                if operation == "generate":
                    roots = {"sample", "previous", "feedback"}
                else:
                    roots = {"sample", "generation", "candidate"}
                if stage_name == "target":
                    roots.add("source")
                for name, value in call.inputs.items():
                    if not re.fullmatch(r"[A-Za-z_]\w*", name):
                        raise ValueError(f"invalid template variable: {name}")
                    validate_binding(value, roots)
        return self

    def require_credentials(self):
        for name, model in self.models.items():
            if model.model == "unset":
                raise ConfigurationError(f"models.{name}.model / MODEL_NAME is required")
            if not self._credentials.get(model.api_key_env):
                raise ConfigurationError(
                    f"missing API key environment variable: {model.api_key_env}"
                )

    def api_key(self, model: ModelSettings):
        return self._credentials.get(model.api_key_env)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse text without executing shell expressions or modifying os.environ."""
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


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys instead of silently replacing configuration."""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"YAML mapping keys must be unique strings: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(text: str):
    try:
        return yaml.load(text, Loader=UniqueLoader)
    except (yaml.YAMLError, ValueError) as error:
        raise ConfigurationError(f"invalid YAML: {error}") from error


def load_settings(
    path: Path, overrides: dict | None = None, env_file: Path = Path(".env")
) -> RecipeSettings:
    environment = {**read_env_file(env_file), **os.environ}
    values = read_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ConfigurationError("recipe must be a YAML mapping")
    models = values.get("models")
    if not isinstance(models, dict):
        raise ConfigurationError("recipe requires a models mapping")
    for name, model in models.items():
        if not isinstance(model, dict):
            raise ConfigurationError(f"models.{name} must be a mapping")
        models[name] = {
            "model": environment.get("MODEL_NAME", "unset"),
            "base_url": environment.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            **interpolate_environment(model, environment),
        }
    runtime = values.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigurationError("runtime must be a mapping")
    runtime.update({key: value for key, value in (overrides or {}).items() if value is not None})
    settings = RecipeSettings.model_validate(values)
    settings._credentials = {
        model.api_key_env: environment.get(model.api_key_env) for model in settings.models.values()
    }
    return settings
