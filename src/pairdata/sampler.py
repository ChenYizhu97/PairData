"""Independent, deterministic sampling from recipe-defined fields."""

import hashlib
import json
import math
import random
import re
from typing import Annotated, Literal

from pydantic import Field, JsonValue, RootModel, model_validator

from pairdata.schemas import Contract


class Choice(Contract):
    value: JsonValue
    weight: float = Field(default=1.0, ge=0)


class Categorical(Contract):
    type: Literal["categorical"]
    choices: list[Choice] = Field(min_length=1)
    dropout: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def check_weights(self):
        total = sum(item.weight for item in self.choices)
        if not math.isfinite(total) or total <= 0:
            raise ValueError("categorical weights must have a finite positive sum")
        values = [json.dumps(item.value, sort_keys=True, allow_nan=False) for item in self.choices]
        if len(set(values)) != len(values):
            raise ValueError("categorical choices must be distinct")
        return self


class Integer(Contract):
    type: Literal["integer"]
    min: int
    max: int
    dropout: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def check_bounds(self):
        if self.min > self.max:
            raise ValueError("integer min must not exceed max")
        return self


SamplerField = Annotated[Categorical | Integer, Field(discriminator="type")]


class SamplerSettings(RootModel[dict[str, SamplerField]]):
    @model_validator(mode="after")
    def check_names(self):
        if any(not re.fullmatch(r"[A-Za-z_]\w*", name) for name in self.root):
            raise ValueError("sampler fields must be simple identifiers")
        return self


def sample_fields(fields: dict[str, SamplerField], seed: int, index: int, stage: str) -> dict:
    sample = {}
    for name, field in fields.items():
        key = json.dumps([seed, index, stage, name]).encode()
        rng = random.Random(hashlib.sha256(key).hexdigest())
        if rng.random() < field.dropout:
            continue
        if isinstance(field, Categorical):
            sample[name] = rng.choices(
                [item.value for item in field.choices], [item.weight for item in field.choices]
            )[0]
        else:
            sample[name] = rng.randint(field.min, field.max)
    return sample
