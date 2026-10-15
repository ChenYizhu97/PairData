"""Load and compile all recipe resources before any model requests."""

import json
from dataclasses import dataclass
from pathlib import Path

from pairdata.config import (
    CallSettings,
    ConfigurationError,
    RecipeSettings,
    StageSettings,
    load_settings,
    read_yaml,
)
from pairdata.prompts import PromptRenderer
from pairdata.rules import Rule
from pairdata.sampler import SamplerSettings
from pairdata.validation import SchemaValidator


@dataclass
class CompiledCall:
    settings: CallSettings
    renderer: PromptRenderer
    validator: SchemaValidator


@dataclass
class CompiledStage:
    settings: StageSettings
    sampler: SamplerSettings
    generate: CompiledCall
    evaluate: CompiledCall
    decision: Rule


@dataclass
class Recipe:
    settings: RecipeSettings
    resources: dict[str, str]
    stages: dict[str, CompiledStage]

    def snapshot(self):
        config = self.settings.model_dump()
        for name, stage in self.stages.items():
            config[name]["sampler"] = stage.sampler.model_dump()
        return {"config": config, "resources": self.resources}


def load_recipe(path: Path, overrides: dict | None = None, env_file: Path = Path(".env")) -> Recipe:
    settings = load_settings(path, overrides, env_file)
    root = path.resolve().parent
    resources = {}

    def read(name):
        resolved = (root / name).resolve()
        if not resolved.is_relative_to(root):
            raise ConfigurationError(f"resource must be inside recipe directory: {name}")
        if name not in resources:
            resources[name] = resolved.read_text(encoding="utf-8")
        return resources[name]

    stages = {}
    for stage_name in ("source", "target"):
        stage = getattr(settings, stage_name)
        try:
            sampler = SamplerSettings.model_validate(read_yaml(read(stage.sampler)))
        except ValueError as error:
            raise ConfigurationError(f"{stage_name}.sampler: {error}") from error
        calls = {}
        for operation in ("generate", "evaluate"):
            call = getattr(stage, operation)
            for message in call.messages:
                read(message.template)
            try:
                schema = json.loads(read(call.response_schema))
                validator = SchemaValidator(
                    schema, call.output_path if operation == "generate" else None
                )
                renderer = PromptRenderer(call, resources)
            except (ValueError, ConfigurationError) as error:
                raise ConfigurationError(f"{stage_name}.{operation}: {error}") from error
            calls[operation] = CompiledCall(call, renderer, validator)
        rule = Rule.model_validate(read_yaml(read(stage.decision)))
        roots = {"sample", "candidate", "generation", "evaluation"}
        if stage_name == "target":
            roots.add("source")
        rule.validate_context(roots)
        stages[stage_name] = CompiledStage(
            stage, sampler, calls["generate"], calls["evaluate"], rule
        )
    return Recipe(settings, resources, stages)
