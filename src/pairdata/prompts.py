"""Render configured messages from explicitly bound, stage-local values."""

import json

from jinja2 import StrictUndefined, TemplateError, meta, nodes
from jinja2.sandbox import SandboxedEnvironment

from pairdata.bindings import resolve
from pairdata.config import CallSettings, ConfigurationError


class PromptRenderer:
    def __init__(self, settings: CallSettings, resources: dict[str, str]):
        self.settings = settings
        environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        environment.globals.clear()
        # Sampling belongs to the sampler; rendering must be reproducible on replay.
        environment.filters.pop("random", None)
        self.templates = []
        for message in settings.messages:
            try:
                text = resources[message.template]
                tree = environment.parse(text)
                if list(
                    tree.find_all((nodes.Include, nodes.Import, nodes.FromImport, nodes.Extends))
                ):
                    raise ConfigurationError(
                        "template includes, imports and inheritance are not supported"
                    )
                unknown = meta.find_undeclared_variables(tree) - settings.inputs.keys()
                if unknown:
                    raise ConfigurationError(
                        f"{message.template}: unbound variables {sorted(unknown)}"
                    )
                self.templates.append((message.role, environment.from_string(text)))
            except TemplateError as error:
                raise ConfigurationError(f"{message.template}: {error}") from error

    def render(self, context: dict, schema: dict) -> tuple[dict, list[dict]]:
        try:
            inputs = {key: resolve(value, context) for key, value in self.settings.inputs.items()}
            messages = [
                {"role": role, "content": template.render(**inputs)}
                for role, template in self.templates
            ]
        except (ValueError, TypeError, TemplateError) as error:
            raise ConfigurationError(f"cannot render model request: {error}") from error
        instruction = (
            "Return only JSON conforming to this JSON Schema, without Markdown:\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        if messages[0]["role"] == "system":
            messages[0]["content"] += "\n" + instruction
        else:
            messages.insert(0, {"role": "system", "content": instruction})
        return inputs, messages
