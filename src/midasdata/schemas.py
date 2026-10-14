"""模型响应和落盘记录。工作流的运行状态定义在各自模块中。

Model responses and persisted records. Each workflow defines its own runtime state.
"""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ExpansionLevel = Literal["light", "medium", "rich"]
EXPANSION_LEVELS: tuple[ExpansionLevel, ...] = ("light", "medium", "rich")
Dimension = Literal["domain", "focus", "specificity", "expression", "language"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Sample(Contract):
    """sampler.py 使用的五维采样契约；未采样的维度为 None。

    The five-dimension sampling contract used by sampler.py; unsampled dimensions are None.
    """

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


class InputPrompt(Contract):
    id: Text
    prompt: Text


class SourcePromptRecord(InputPrompt):
    sample: Sample

    @field_serializer("sample")
    def serialize_conditions(self, value: Sample) -> dict:
        return value.model_dump(exclude_none=True)


class ExpandedPromptRecord(InputPrompt):
    # level 统一表示 Light、Medium、Rich 扩写等级。
    # level consistently denotes the Light, Medium or Rich expansion level.
    level: ExpansionLevel


class SourceDraft(Contract):
    concept: Text
    prompt: Text


class ExpansionDraft(Contract):
    prompt: Text


class ConditionEvidence(Contract):
    satisfied: bool
    evidence: Text


class SourceValidation(Contract):
    conditions: dict[Dimension, ConditionEvidence]
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


class ExpansionValidation(LevelValidation):
    original_pius: list[OriginalPIU]


class ExpansionSetValidation(Contract):
    """三个档位共享一次源事实提取；字段与固定的组校验提示词一致。

    All three levels share source facts; fields match the fixed group validation prompt.
    """

    original_pius: list[OriginalPIU]
    levels: dict[ExpansionLevel, LevelValidation]
    retry_levels: list[ExpansionLevel]
    insufficient_expansion_space: bool
    feedback: str


class QualityDecision(Contract):
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    added_count: int = 0
    retry_levels: list[ExpansionLevel] = Field(default_factory=list)
    insufficient_expansion_space: bool = False


class ExpansionResult(Contract):
    level: ExpansionLevel
    round: int
    generation_attempt: int
    status: Literal["passed", "quality_failed", "technical_failed"]
    prompt: str = ""
    validation: ExpansionValidation | None = None
    decision: QualityDecision | None = None
    reason: str = ""


class SourceGenerationDetails(Contract):
    prompt: str = ""
    generation_attempt: int = 0
    validation: SourceValidation | None = None
    decision: QualityDecision | None = None
    feedback: str = ""


class PromptExpansionDetails(Contract):
    round: int = 0
    levels: dict[ExpansionLevel, ExpansionResult] = Field(default_factory=dict)
    group_validation: ExpansionSetValidation | None = None
    group_decision: QualityDecision | None = None
    feedback: dict[ExpansionLevel, str] = Field(default_factory=dict)


class TaskResult(Contract):
    """一个任务的终态；扩写任务以整组三档为提交单位。

    The final task result; expansion tasks commit all three levels together.
    """

    id: Text
    workflow: Literal["generate", "expand"]
    status: Literal["passed", "rejected"]
    source: dict
    records: list[SourcePromptRecord | ExpandedPromptRecord] = Field(default_factory=list)
    concept: str = ""
    reason: str = ""
    details: SourceGenerationDetails | PromptExpansionDetails

    @model_validator(mode="after")
    def validate_workflow_records(self):
        if self.workflow == "generate":
            record_type, details_type = SourcePromptRecord, SourceGenerationDetails
        else:
            record_type, details_type = ExpandedPromptRecord, PromptExpansionDetails
        if any(not isinstance(record, record_type) for record in self.records):
            raise ValueError(f"{self.workflow} requires {record_type.__name__} records")
        if not isinstance(self.details, details_type):
            raise ValueError(f"{self.workflow} requires {details_type.__name__} details")
        return self


class OperationEvent(Contract):
    run_id: str
    sample_id: str
    event: Literal[
        "request", "response", "success", "quality_decision", "technical_failure", "fatal"
    ]
    timestamp: str
    stage: str
    level: ExpansionLevel | None = None
    round: int = 0
    logical_call_id: str
    technical_attempt: int = 0
    generation_attempt: int = 0
    model: str = ""
    data: dict = Field(default_factory=dict)
