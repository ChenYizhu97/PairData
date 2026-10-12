"""Independent weighted dimensions with stable, pre-execution sampling."""

import json
import math
import random
from importlib.resources import files
from pathlib import Path
from typing import get_args

from midasdata.schemas import Dimension, Sample

DIMENSIONS = get_args(Dimension)


def load_sampler(path: Path | None = None) -> dict:
    text = (
        path.read_text(encoding="utf-8")
        if path
        else files("midasdata").joinpath("sampler.json").read_text(encoding="utf-8")
    )
    options = json.loads(text)
    if not isinstance(options, dict) or set(options) != set(DIMENSIONS):
        raise ValueError("sampler requires exactly: " + ", ".join(DIMENSIONS))
    for dimension, rows in options.items():
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{dimension}: options cannot be empty")
        seen, weights = set(), []
        for row in rows:
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError(f"{dimension}: expected [option, explanation, weight]")
            key, explanation, weight = row
            if not isinstance(key, str):
                raise ValueError(f"{dimension}: option must be a non-null string")
            Sample.model_validate({dimension: key})
            if key in seen or not isinstance(explanation, str) or not explanation.strip():
                raise ValueError(f"{dimension}: duplicate option or empty explanation")
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(weight)
                or weight < 0
            ):
                raise ValueError(f"{dimension}: invalid weight")
            seen.add(key)
            weights.append(weight)
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9, rel_tol=0):
            raise ValueError(f"{dimension}: weights must sum to 1")
    return options


def sample_conditions(rng: random.Random, options: dict, dropout: float = 0.1) -> dict:
    if not 0 <= dropout <= 1:
        raise ValueError("dropout must be between 0 and 1")
    sample = {}
    for dimension in DIMENSIONS:
        if rng.random() >= dropout:
            rows = options[dimension]
            sample[dimension] = rng.choices([row[0] for row in rows], [row[2] for row in rows])[0]
    return sample


def explain(sample: dict, options: dict) -> dict:
    return {
        dimension: {
            "value": key,
            "meaning": next(row[1] for row in options[dimension] if row[0] == key),
        }
        for dimension, key in sample.items()
    }
