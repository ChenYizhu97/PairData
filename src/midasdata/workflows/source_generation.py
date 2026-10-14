"""源描述生成：初始化 → 生成草稿 → 校验 → 接受 / 再生成 / 拒绝。

Source generation: initialize → draft → validate → accept / regenerate / reject.
"""

from functools import partial
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from midasdata.execution import ExecutionContext, ExecutionFailed, execute, operation_metadata
from midasdata.limits import MAX_DRAFTS, MAX_REQUEST_ATTEMPTS
from midasdata.model import LLMModel
from midasdata.prompts import messages
from midasdata.sampler import explain
from midasdata.schemas import (
    Dimension,
    QualityDecision,
    SourceDraft,
    SourceGenerationDetails,
    SourcePromptRecord,
    SourceValidation,
    TaskResult,
)
from midasdata.validation import assess_source


class SourceGenerationInput(TypedDict):
    task_id: str
    conditions: dict[Dimension, str]


class SourceGenerationState(SourceGenerationInput, total=False):
    concept: str
    draft_prompt: str
    draft_count: int
    validation: SourceValidation | None
    decision: QualityDecision | None
    revision_feedback: str
    failure_reason: str | None
    result: TaskResult


class SourceGenerationOutput(TypedDict):
    result: TaskResult


# 节点：只返回当前节点更新的字段，不修改共享的输入 state。
# Nodes return only updated fields and leave the shared input state unchanged.
def initialize_source(state: SourceGenerationState) -> dict:
    return {
        "concept": "",
        "draft_prompt": "",
        "draft_count": 0,
        "validation": None,
        "decision": None,
        "revision_feedback": "",
        "failure_reason": None,
    }


async def draft_source(
    state: SourceGenerationState,
    *,
    generator: LLMModel,
    execution: ExecutionContext,
    sampler: dict,
) -> dict:
    draft_number = state["draft_count"] + 1
    payload = {
        "sample": state["conditions"],
        "conditions": explain(state["conditions"], sampler),
        "concept": state["concept"],
        "previous_prompt": state["draft_prompt"],
        "feedback": state["revision_feedback"],
    }
    metadata = operation_metadata(
        state["task_id"], "generate", model=generator.name, draft_number=draft_number
    )
    try:
        draft = await execute(
            partial(generator.invoke, messages("generate", payload, SourceDraft), SourceDraft),
            context=execution,
            metadata=metadata,
            payload=payload,
            parse=partial(generator.parse, schema=SourceDraft),
            retry_if=generator.is_retryable,
            describe_error=generator.error_text,
            max_attempts=MAX_REQUEST_ATTEMPTS,
        )
    except ExecutionFailed as error:
        # 未产生替代草稿，上一版文本和对应证据仍然有效。
        # No replacement draft was produced, so the previous text and its evidence remain valid.
        return {"draft_count": draft_number, "failure_reason": f"technical_failure:{error}"}
    return {
        "concept": draft.concept,
        "draft_prompt": draft.prompt,
        "draft_count": draft_number,
        "validation": None,
        "decision": None,
        "revision_feedback": "",
        "failure_reason": None,
    }


async def blind_sampler_validation(
    state: SourceGenerationState,
    *,
    validator: LLMModel,
    execution: ExecutionContext,
    sampler: dict,
) -> dict:
    payload = {
        "sample": state["conditions"],
        "conditions": explain(state["conditions"], sampler),
        "prompt": state["draft_prompt"],
    }
    metadata = operation_metadata(
        state["task_id"],
        "blind_sampler_validation",
        model=validator.name,
        draft_number=state["draft_count"],
    )
    try:
        report = await execute(
            partial(
                validator.invoke,
                messages("blind_sampler_validation", payload, SourceValidation),
                SourceValidation,
            ),
            context=execution,
            metadata=metadata,
            payload=payload,
            parse=partial(validator.parse, schema=SourceValidation),
            retry_if=validator.is_retryable,
            describe_error=validator.error_text,
            max_attempts=MAX_REQUEST_ATTEMPTS,
        )
    except ExecutionFailed as error:
        return {"failure_reason": f"technical_failure:{error}"}
    # 模型只提供报告；节点按业务规则计算质量结果并记录。
    # The model supplies a report; the node computes and records the business quality decision.
    decision = assess_source(report, state["conditions"], state["draft_prompt"])
    execution.store.record_decision(metadata, decision)
    return {
        "validation": report,
        "decision": decision,
        "revision_feedback": "\n".join([report.feedback, "; ".join(decision.reasons)]),
    }


def source_result(state: SourceGenerationState, passed: bool) -> TaskResult:
    source = {"id": state["task_id"], "sample": state["conditions"]}
    return TaskResult(
        id=state["task_id"],
        workflow="generate",
        status="passed" if passed else "rejected",
        source=source,
        concept=state["concept"],
        records=[SourcePromptRecord(**source, prompt=state["draft_prompt"])] if passed else [],
        reason="" if passed else state["failure_reason"] or "quality_budget_exhausted",
        details=SourceGenerationDetails(
            prompt=state["draft_prompt"],
            generation_attempt=state["draft_count"],
            validation=state["validation"],
            decision=state["decision"],
            feedback=state["revision_feedback"],
        ),
    )


def accept_source(state: SourceGenerationState) -> dict:
    return {"result": source_result(state, True)}


def reject_source(state: SourceGenerationState) -> dict:
    return {"result": source_result(state, False)}


# 路由：只决定下一个节点，不产生状态更新或副作用。
# Routes select the next node without state updates or side effects.
def after_source_draft(
    state: SourceGenerationState,
) -> Literal["blind_sampler_validation", "reject_source"]:
    return "reject_source" if state["failure_reason"] is not None else "blind_sampler_validation"


def after_source_validation(
    state: SourceGenerationState,
) -> Literal["accept_source", "draft_source", "reject_source"]:
    if state["failure_reason"] is not None:
        return "reject_source"
    decision = state["decision"]
    assert decision is not None, "source routing requires current validation"
    if decision.passed:
        return "accept_source"
    return "draft_source" if state["draft_count"] < MAX_DRAFTS else "reject_source"


# build：所有节点和边在这里直接可见。
# Build: all nodes and edges are directly visible here.
def build_source_generation(
    generator: LLMModel, validator: LLMModel, execution: ExecutionContext, sampler: dict
):
    graph = StateGraph(
        SourceGenerationState,
        input_schema=SourceGenerationInput,
        output_schema=SourceGenerationOutput,
    )
    graph.add_node("initialize_source", initialize_source)
    graph.add_node(
        "draft_source",
        partial(draft_source, generator=generator, execution=execution, sampler=sampler),
    )
    graph.add_node(
        "blind_sampler_validation",
        partial(
            blind_sampler_validation, validator=validator, execution=execution, sampler=sampler
        ),
    )
    graph.add_node("accept_source", accept_source)
    graph.add_node("reject_source", reject_source)
    graph.add_edge(START, "initialize_source")
    graph.add_edge("initialize_source", "draft_source")
    graph.add_conditional_edges("draft_source", after_source_draft)
    graph.add_conditional_edges("blind_sampler_validation", after_source_validation)
    graph.add_edge("accept_source", END)
    graph.add_edge("reject_source", END)
    return graph.compile()
