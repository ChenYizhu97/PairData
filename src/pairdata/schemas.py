"""Fixed runtime contracts. Recipe JSON Schemas describe model responses."""

from dataclasses import dataclass
from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
StageName = Literal["source", "target"]
OperationName = Literal["sample", "generate", "evaluate", "gate"]
CallName = Literal["generate", "evaluate"]


class ModelResponse(TypedDict):
    raw: str | list
    parsed: NotRequired[JsonValue]
    usage: NotRequired[dict]


@dataclass(frozen=True)
class OperationMetadata:
    task_id: str
    operation: str
    logical_call_id: str
    generation_attempt: int = 0
    model: str = ""


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class QualityDecision(Contract):
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    checks: list[dict] = Field(default_factory=list)


class StageResult(Contract):
    sample: dict[str, JsonValue] = Field(default_factory=dict)
    generation_attempt: int = 0
    generation: JsonValue = None
    candidate: JsonValue = None
    evaluation: JsonValue = None
    decision: QualityDecision | None = None
    status: Literal["pending", "passed", "quality_failed", "technical_failed"] = "pending"
    reason: str = ""


class TaskResult(Contract):
    id: Text
    status: Literal["passed", "rejected"]
    source: StageResult
    target: StageResult | None = None
    reason: str = ""

    @model_validator(mode="after")
    def check_passed(self):
        if self.status == "passed":
            for stage in (self.source, self.target):
                if (
                    stage is None
                    or stage.status != "passed"
                    or not stage.decision
                    or not stage.decision.passed
                ):
                    raise ValueError("passed tasks require two accepted stages")
        return self


class OperationEvent(Contract):
    # Keep the existing audit format readable across this internal naming change.
    model_config = ConfigDict(populate_by_name=True)
    run_id: str
    task_id: str = Field(alias="sample_id")
    event: Literal[
        "sample", "request", "response", "success", "quality_decision", "technical_failure", "fatal"
    ]
    timestamp: str
    operation: str = Field(alias="stage")
    logical_call_id: str
    request_attempt: int = Field(default=0, alias="technical_attempt")
    generation_attempt: int = 0
    model: str = ""
    data: dict = Field(default_factory=dict)
