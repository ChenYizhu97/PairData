"""Business contracts, model responses, and internal graph state."""

from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Level = Literal["light", "medium", "rich"]
LEVELS: tuple[Level, ...] = ("light", "medium", "rich")
Dimension = Literal["domain", "focus", "specificity", "expression", "language"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Sample(Contract):
    domain: (
        Literal[
            "people",
            "animals",
            "objects",
            "scenes",
            "plants_food",
            "vehicles",
            "art_design",
            "cultural_abstract",
        ]
        | None
    ) = None
    focus: Literal["subject", "action", "scene", "relation", "composition", "aesthetic"] | None = (
        None
    )
    specificity: Literal["minimal", "moderate"] | None = None
    expression: Literal["keywords", "natural", "colloquial"] | None = None
    language: Literal["zh", "en", "mixed"] | None = None


class SourceRecord(Contract):
    id: Text
    prompt: Text


class GeneratedRecord(SourceRecord):
    sample: Sample

    @field_serializer("sample")
    def serialize_sample(self, value: Sample) -> dict:
        return value.model_dump(exclude_none=True)


class ExpandedRecord(SourceRecord):
    level: Level


class SourceDraft(Contract):
    concept: Text
    prompt: Text


class EnglishDraft(Contract):
    prompt: Text


class ConditionCheck(Contract):
    satisfied: bool
    evidence: Text


class SourceValidation(Contract):
    conditions: dict[Dimension, ConditionCheck]
    visual: bool
    clean: bool
    visual_evidence: Text
    feedback: str


class OriginalPIU(Contract):
    fact: Text
    evidence: Text


class PreservedPIU(Contract):
    original_index: Annotated[int, Field(ge=0)]
    output_evidence: Text


class AddedPIU(Contract):
    fact: Text
    output_evidence: Text
    relevant: bool
    novel: bool
    visual: bool


class LevelValidation(Contract):
    """Group-level evidence refers to the group's shared original PIU list."""

    semantic_preservation: bool
    no_contradiction: bool
    meaningful_addition: bool
    valid_format: bool
    preserved_pius: list[PreservedPIU]
    missing_pius: list[str]
    contradictions: list[str]
    added_pius: list[AddedPIU]
    insufficient_expansion_space: bool
    feedback: str


class EnglishValidation(LevelValidation):
    """Standalone validation must extract its own original PIUs."""

    original_pius: list[OriginalPIU]


class GroupValidation(Contract):
    original_pius: list[OriginalPIU]
    levels: dict[Level, LevelValidation]
    retry_levels: list[Level]
    insufficient_expansion_space: bool
    feedback: str


class Decision(Contract):
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    added_count: int = 0
    retry_levels: list[Level] = Field(default_factory=list)
    insufficient_expansion_space: bool = False


class LevelResult(Contract):
    level: Level
    round: int
    generation_attempt: int
    status: Literal["passed", "quality_failed", "technical_failed"]
    prompt: str = ""
    validation: EnglishValidation | None = None
    decision: Decision | None = None
    reason: str = ""


class GenerateDetails(Contract):
    prompt: str = ""
    validation: SourceValidation | None = None
    decision: Decision | None = None
    generation_attempt: int = 0
    feedback: str = ""


class ExpandDetails(Contract):
    round: int = 0
    levels: dict[Level, LevelResult] = Field(default_factory=dict)
    group_validation: GroupValidation | None = None
    group_decision: Decision | None = None
    feedback: dict[Level, str] = Field(default_factory=dict)


class SampleResult(Contract):
    id: Text
    workflow: Literal["generate", "expand"]
    status: Literal["passed", "rejected"]
    source: dict
    records: list[GeneratedRecord | ExpandedRecord] = Field(default_factory=list)
    concept: str = ""
    reason: str = ""
    details: GenerateDetails | ExpandDetails = Field(default_factory=GenerateDetails)

    @model_validator(mode="before")
    @classmethod
    def select_workflow_types(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if value.get("workflow") == "generate":
            record_type, details_type = GeneratedRecord, GenerateDetails
        elif value.get("workflow") == "expand":
            record_type, details_type = ExpandedRecord, ExpandDetails
        else:
            return value
        records = value.get("records", [])
        if not isinstance(records, list):
            raise ValueError("records must be a list")
        return {
            **value,
            "records": [record_type.model_validate(row) for row in records],
            "details": details_type.model_validate(value.get("details", {})),
        }


class AuditEvent(Contract):
    run_id: str
    sample_id: str
    event: str
    timestamp: str
    stage: str
    level: Level | None = None
    round: int = 0
    logical_call_id: str
    technical_attempt: int = 0
    generation_attempt: int = 0
    model: str = ""
    data: dict = Field(default_factory=dict)


def merge_levels(
    left: dict[Level, LevelResult], right: dict[Level, LevelResult]
) -> dict[Level, LevelResult]:
    """Replace by level; late results may never overwrite a newer round."""
    merged = dict(left)
    for level, value in right.items():
        if level not in merged or value.round >= merged[level].round:
            merged[level] = value
    return merged


class GenerateState(TypedDict, total=False):
    id: str
    sample: dict[Dimension, str]
    concept: str
    prompt: str
    generation_attempt: int
    validation: SourceValidation | None
    decision: Decision | None
    feedback: str
    reason: str
    result: SampleResult


class ExpandInput(TypedDict):
    id: str
    prompt: str


class ExpandUpdate(TypedDict, total=False):
    round: int
    pending: list[Level]
    levels: Annotated[dict[Level, LevelResult], merge_levels]
    feedback: dict[Level, str]
    group_validation: GroupValidation | None
    group_decision: Decision | None
    reason: str
    result: SampleResult


class ExpandState(ExpandInput, ExpandUpdate):
    pass


class LevelState(TypedDict, total=False):
    id: str
    prompt: str
    level: Level
    round: int
    generation_attempt: int
    feedback: str
    previous_output: str
    output: str
    result: LevelResult | None


class CallContext(TypedDict):
    sample_id: str
    level: NotRequired[Level]
    round: NotRequired[int]
    generation_attempt: NotRequired[int]
