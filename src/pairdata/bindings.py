"""Simple data paths and explicit input bindings; no expression evaluation."""

import re
from copy import deepcopy


class PathError(ValueError):
    pass


def path_parts(path: str) -> list[str | int]:
    if path == "$":
        return []
    if not isinstance(path, str) or not re.fullmatch(
        r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[\d+\])*", path
    ):
        raise ValueError(f"invalid field path: {path!r}")
    return [
        int(index) if index else name
        for name, index in re.findall(r"([A-Za-z_]\w*)|\[(\d+)\]", path)
    ]


def lookup(data, path: str):
    value = data
    for part in path_parts(path):
        if isinstance(part, int):
            if not isinstance(value, list):
                raise TypeError(f"{path}: expected an array")
            if part >= len(value):
                raise PathError(f"missing field: {path}")
        else:
            if not isinstance(value, dict):
                raise TypeError(f"{path}: expected an object")
            if part not in value:
                raise PathError(f"missing field: {path}")
        value = value[part]
    return value


def validate_binding(value, roots: set[str]) -> None:
    if isinstance(value, dict) and "ref" in value:
        if set(value) != {"ref"}:
            raise ValueError("a reference must contain only ref")
        parts = path_parts(value["ref"])
        if not parts or parts[0] not in roots:
            raise ValueError(f"reference is not available here: {value['ref']}")


def resolve(value, context: dict):
    if isinstance(value, dict) and "ref" in value:
        return deepcopy(lookup(context, value["ref"]))
    return deepcopy(value)
