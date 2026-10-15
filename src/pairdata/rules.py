"""A small declarative decision interpreter with explicit, auditable operators."""

import operator
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from pairdata.bindings import PathError, lookup, path_parts, resolve, validate_binding
from pairdata.schemas import Contract, QualityDecision


class Rule(Contract):
    all_: list["Rule"] | None = Field(default=None, alias="all", min_length=1)
    any_: list["Rule"] | None = Field(default=None, alias="any", min_length=1)
    not_: "Rule | None" = Field(default=None, alias="not")
    field: str | None = None
    op: (
        Literal["eq", "ne", "gt", "gte", "lt", "lte", "exists", "in", "contains", "length_between"]
        | None
    ) = None
    value: JsonValue = None
    min: JsonValue = None
    max: JsonValue = None

    @model_validator(mode="after")
    def check_shape(self):
        keys = self.model_fields_set
        for key in ("all_", "any_", "not_"):
            if key in keys:
                if keys != {key} or getattr(self, key) is None:
                    raise ValueError("logical rules must contain exactly one non-null operator")
                return self
        required = {"field", "op"}
        if self.op == "length_between":
            required |= {"min", "max"}
            for bound in (self.min, self.max):
                if isinstance(bound, dict):
                    if set(bound) != {"ref"}:
                        raise ValueError("length bound objects must be references")
                    path_parts(bound["ref"])
                elif type(bound) is not int or bound < 0:
                    raise ValueError("length bounds must be nonnegative integers or references")
            if type(self.min) is int and type(self.max) is int and self.min > self.max:
                raise ValueError("length min must not exceed max")
        elif self.op != "exists":
            required.add("value")
        if keys != required or self.op is None or self.field is None:
            raise ValueError("invalid fields for rule operator")
        path_parts(self.field)
        return self

    def validate_context(self, roots: set[str]):
        for children in (self.all_, self.any_):
            if children is not None:
                for child in children:
                    child.validate_context(roots)
                return
        if self.not_ is not None:
            self.not_.validate_context(roots)
            return
        validate_binding({"ref": self.field}, roots)
        for value in (self.value, self.min, self.max):
            validate_binding(value, roots)


def numeric(value):
    return type(value) in (int, float)


def equal(left, right):
    if numeric(left) and numeric(right):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
    return left == right


def compare(op, actual, expected):
    if op in {"eq", "ne"}:
        if type(actual) is not type(expected) and not (numeric(actual) and numeric(expected)):
            raise TypeError("comparison operands have different types")
        result = equal(actual, expected)
        return result if op == "eq" else not result
    if op in {"gt", "gte", "lt", "lte"}:
        if not numeric(actual) or not numeric(expected):
            raise TypeError("ordering requires numbers")
        return {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}[op](
            actual, expected
        )
    if op == "in":
        if not isinstance(expected, list):
            raise TypeError("in requires an array of allowed values")
        return any(equal(actual, item) for item in expected)
    if op == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, list):
            return any(equal(item, expected) for item in actual)
        raise TypeError("contains requires a string and substring, or an array")
    raise ValueError(f"unsupported comparison: {op}")


def evaluate_rule(rule: Rule, context: dict) -> QualityDecision:
    checks, errors = [], []

    def evaluate(node, location):
        if node.all_ is not None or node.any_ is not None:
            is_all = node.all_ is not None
            children = node.all_ if is_all else node.any_
            op = "all" if is_all else "any"
            values = [
                evaluate(child, f"{location}.{op}[{index}]") for index, child in enumerate(children)
            ]
            passed = all(values) if is_all else any(values)
            checks.append({"path": location, "op": op, "passed": passed})
            return passed
        if node.not_ is not None:
            passed = not evaluate(node.not_, location + ".not")
            checks.append({"path": location, "op": "not", "passed": passed})
            return passed
        check = {"path": location, "field": node.field, "op": node.op, "passed": False}
        try:
            if node.op == "exists":
                try:
                    lookup(context, node.field)
                    check["passed"] = True
                except PathError:
                    pass
            else:
                actual = lookup(context, node.field)
                check["actual"] = actual
                if node.op == "length_between":
                    low, high = resolve(node.min, context), resolve(node.max, context)
                    check["expected"] = {"min": low, "max": high}
                    if not isinstance(actual, (str, list)):
                        raise TypeError("length_between requires a string or array")
                    if type(low) is not int or type(high) is not int or not 0 <= low <= high:
                        raise TypeError("length bounds must be ordered nonnegative integers")
                    check["passed"] = low <= len(actual) <= high
                else:
                    expected = resolve(node.value, context)
                    check["expected"] = expected
                    check["passed"] = compare(node.op, actual, expected)
        except (ValueError, TypeError) as error:
            check["error"] = str(error)
            errors.append(f"{location}: {error}")
        checks.append(check)
        return check["passed"]

    condition = evaluate(rule, "rule")
    passed = condition and not errors
    reasons = errors or (
        []
        if passed
        else [f"{item['path']}: condition failed" for item in checks if not item["passed"]]
    )
    return QualityDecision(passed=passed, reasons=reasons, checks=checks)
