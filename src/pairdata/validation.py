"""Compile response JSON Schemas and validate fresh or replayed model output."""

import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from pairdata.bindings import lookup, path_parts
from pairdata.config import ConfigurationError


class SchemaValidator:
    def __init__(self, schema: dict, output_path: str | None = None):
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ConfigurationError("response schemas must describe a JSON object")
        dialect = schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
        if dialect != "https://json-schema.org/draft/2020-12/schema":
            raise ConfigurationError("only JSON Schema Draft 2020-12 is supported")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise ConfigurationError(f"invalid JSON Schema: {error.message}") from error

        def check_refs(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"$ref", "$dynamicRef"}:
                        if not isinstance(item, str) or not item.startswith("#/"):
                            raise ConfigurationError(
                                "only local #/ JSON Schema references are supported"
                            )
                        target = schema
                        try:
                            for part in item[2:].split("/"):
                                part = part.replace("~1", "/").replace("~0", "~")
                                target = (
                                    target[int(part)] if isinstance(target, list) else target[part]
                                )
                        except (KeyError, TypeError, IndexError, ValueError):
                            raise ConfigurationError(
                                f"unresolved schema reference: {item}"
                            ) from None
                    else:
                        check_refs(item)
            elif isinstance(value, list):
                for item in value:
                    check_refs(item)

        check_refs(schema)
        self.schema = schema
        self.validator = Draft202012Validator(schema)
        self.output_path = output_path
        if output_path:
            current = schema
            for part in path_parts(output_path):
                if not isinstance(current, dict) or "$ref" in current:
                    break
                if isinstance(part, int):
                    current = current.get("items", {})
                else:
                    properties = current.get("properties", {})
                    if part not in properties and current.get("additionalProperties") is False:
                        raise ConfigurationError(
                            f"output_path is absent from response schema: {output_path}"
                        )
                    current = properties.get(part, {})

    def parse(self, data: dict):
        try:
            result = data.get("parsed")
            if result is None:
                result = json.loads(data["raw"])
            # JSON forbids NaN/Infinity, including in unconstrained properties.
            json.dumps(result, allow_nan=False)
            self.validator.validate(result)
            if self.output_path is not None:
                lookup(result, self.output_path)
            return result
        except ValidationError as error:
            path = ".".join(map(str, error.absolute_path)) or "$"
            raise ValueError(f"response schema at {path}: {error.message}") from error
        except (TypeError, KeyError) as error:
            raise ValueError(f"invalid model response: {error}") from error
