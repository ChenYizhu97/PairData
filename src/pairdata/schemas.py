"""Fixed runtime contracts. Recipe JSON Schemas describe model responses."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


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
    run_id: str
    sample_id: str
    event: Literal[
        "sample", "request", "response", "success", "quality_decision", "technical_failure", "fatal"
    ]
    timestamp: str
    stage: str
    logical_call_id: str
    technical_attempt: int = 0
    generation_attempt: int = 0
    model: str = ""
    data: dict = Field(default_factory=dict)
