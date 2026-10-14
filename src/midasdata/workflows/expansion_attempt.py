"""指定扩写等级的一次尝试：初始化 → 生成 → 校验 → 返回结果。

One expansion attempt at a given level: initialize → generate → validate → return result.

这里没有质量修订循环；三档编排统一分配次数，单档与组修订共享预算。
The parent workflow owns revisions; individual and group revisions share each level's budget.
"""

from functools import partial
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from midasdata.config import ExpansionRanges
from midasdata.execution import ExecutionContext, ExecutionFailed, execute, operation_metadata
from midasdata.limits import MAX_REQUEST_ATTEMPTS
from midasdata.model import LLMModel
from midasdata.prompts import messages
from midasdata.schemas import (
    ExpansionDraft,
    ExpansionLevel,
    ExpansionResult,
    ExpansionValidation,
    QualityDecision,
)
from midasdata.validation import assess_expansion


class ExpansionAttemptInput(TypedDict):
    task_id: str
    source_prompt: str
    level: ExpansionLevel
    round_number: int
    draft_number: int
    previous_prompt: str
    revision_feedback: str


class ExpansionAttemptState(ExpansionAttemptInput, total=False):
    draft_prompt: str
    validation: ExpansionValidation | None
    decision: QualityDecision | None
    failure_reason: str | None
    result: ExpansionResult


class ExpansionAttemptOutput(TypedDict):
    result: ExpansionResult


# 节点 / Nodes
def initialize_attempt(state: ExpansionAttemptState) -> dict:
    return {"draft_prompt": "", "validation": None, "decision": None, "failure_reason": None}


def attempt_metadata(state: ExpansionAttemptState, stage: str, model: str) -> dict:
    return operation_metadata(
        state["task_id"],
        stage,
        model=model,
        level=state["level"],
        round_number=state["round_number"],
        draft_number=state["draft_number"],
    )


async def draft_expansion(
    state: ExpansionAttemptState,
    *,
    generator: LLMModel,
    execution: ExecutionContext,
    ranges: ExpansionRanges,
) -> dict:
    payload = {
        "source": state["source_prompt"],
        "level": state["level"],
        "piu_range": getattr(ranges, state["level"]).model_dump(),
        "previous_output": state["previous_prompt"],
        "feedback": state["revision_feedback"],
    }
    try:
        draft = await execute(
            partial(generator.invoke, messages("expand", payload, ExpansionDraft), ExpansionDraft),
            context=execution,
            metadata=attempt_metadata(state, "expand", generator.name),
            payload=payload,
            parse=partial(generator.parse, schema=ExpansionDraft),
            retry_if=generator.is_retryable,
            describe_error=generator.error_text,
            max_attempts=MAX_REQUEST_ATTEMPTS,
        )
    except ExecutionFailed as error:
        return {"failure_reason": f"technical_failure:{error}"}
    return {"draft_prompt": draft.prompt}


async def validate_expansion(
    state: ExpansionAttemptState,
    *,
    validator: LLMModel,
    execution: ExecutionContext,
    ranges: ExpansionRanges,
) -> dict:
    payload = {
        "source": state["source_prompt"],
        "output": state["draft_prompt"],
        "level": state["level"],
        "piu_range": getattr(ranges, state["level"]).model_dump(),
    }
    metadata = attempt_metadata(state, "validate_english", validator.name)
    try:
        report = await execute(
            partial(
                validator.invoke,
                messages("validate_english", payload, ExpansionValidation),
                ExpansionValidation,
            ),
            context=execution,
            metadata=metadata,
            payload=payload,
            parse=partial(validator.parse, schema=ExpansionValidation),
            retry_if=validator.is_retryable,
            describe_error=validator.error_text,
            max_attempts=MAX_REQUEST_ATTEMPTS,
        )
    except ExecutionFailed as error:
        return {"failure_reason": f"technical_failure:{error}"}
    decision = assess_expansion(
        report, state["source_prompt"], state["draft_prompt"], state["level"], ranges
    )
    execution.store.record_decision(metadata, decision)
    return {"validation": report, "decision": decision}


def finish_attempt(state: ExpansionAttemptState) -> dict:
    decision = state["decision"]
    if state["failure_reason"] is not None:
        status = "technical_failed"
        reason = state["failure_reason"]
    else:
        assert decision is not None, "completed expansion requires current validation"
        status = "passed" if decision.passed else "quality_failed"
        reason = "; ".join(decision.reasons)
    return {
        "result": ExpansionResult(
            level=state["level"],
            round=state["round_number"],
            generation_attempt=state["draft_number"],
            status=status,
            prompt=state["draft_prompt"],
            validation=state["validation"],
            decision=decision,
            reason=reason,
        )
    }


# 路由 / Routing
def after_expansion_draft(
    state: ExpansionAttemptState,
) -> Literal["validate_expansion", "finish_attempt"]:
    return "finish_attempt" if state["failure_reason"] is not None else "validate_expansion"


# 构建工作流 / Build workflow
def build_expansion_attempt(
    generator: LLMModel, validator: LLMModel, execution: ExecutionContext, ranges: ExpansionRanges
):
    graph = StateGraph(
        ExpansionAttemptState,
        input_schema=ExpansionAttemptInput,
        output_schema=ExpansionAttemptOutput,
    )
    graph.add_node("initialize_attempt", initialize_attempt)
    graph.add_node(
        "draft_expansion",
        partial(draft_expansion, generator=generator, execution=execution, ranges=ranges),
    )
    graph.add_node(
        "validate_expansion",
        partial(validate_expansion, validator=validator, execution=execution, ranges=ranges),
    )
    graph.add_node("finish_attempt", finish_attempt)
    graph.add_edge(START, "initialize_attempt")
    graph.add_edge("initialize_attempt", "draft_expansion")
    graph.add_conditional_edges("draft_expansion", after_expansion_draft)
    graph.add_edge("validate_expansion", "finish_attempt")
    graph.add_edge("finish_attempt", END)
    return graph.compile()
