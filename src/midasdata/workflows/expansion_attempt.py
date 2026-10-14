"""指定扩写等级的一次尝试：初始化 → 生成 → 校验 → 返回结果。

One expansion attempt at a given level: initialize → generate → validate → return result.

三档编排分配目标数量和生成次数，每档独立使用自己的生成预算。
The parent workflow assigns target counts and attempts; each level has its own generation budget.
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
    BlindExpansionValidation,
    ExpansionDraft,
    ExpansionLevel,
    ExpansionResult,
    QualityDecision,
)
from midasdata.validation import assess_expansion


class ExpansionAttemptInput(TypedDict):
    task_id: str
    source_prompt: str
    level: ExpansionLevel
    round_number: int
    draft_number: int
    target_added_pius: int


class ExpansionAttemptState(ExpansionAttemptInput, total=False):
    draft_prompt: str
    validation: BlindExpansionValidation | None
    decision: QualityDecision | None
    failure_reason: str | None
    result: ExpansionResult


class ExpansionAttemptOutput(TypedDict):
    result: ExpansionResult


# 节点 / Nodes
def initialize_attempt(state: ExpansionAttemptState) -> dict:
    return {
        "draft_prompt": "",
        "validation": None,
        "decision": None,
        "failure_reason": None,
    }


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
) -> dict:
    payload = {
        "source": state["source_prompt"],
        "target_added_pius": state["target_added_pius"],
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


async def blind_expand_validation(
    state: ExpansionAttemptState,
    *,
    validator: LLMModel,
    execution: ExecutionContext,
    ranges: ExpansionRanges,
) -> dict:
    payload = {
        "source": state["source_prompt"],
        "output": state["draft_prompt"],
    }
    metadata = attempt_metadata(state, "blind_expand_validation", validator.name)
    try:
        report = await execute(
            partial(
                validator.invoke,
                messages("blind_expand_validation", payload, BlindExpansionValidation),
                BlindExpansionValidation,
            ),
            context=execution,
            metadata=metadata,
            payload=payload,
            parse=partial(validator.parse, schema=BlindExpansionValidation),
            retry_if=validator.is_retryable,
            describe_error=validator.error_text,
            max_attempts=MAX_REQUEST_ATTEMPTS,
        )
    except ExecutionFailed as error:
        return {"failure_reason": f"technical_failure:{error}"}
    decision = assess_expansion(report, state["draft_prompt"], getattr(ranges, state["level"]))
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
            target_added_pius=state["target_added_pius"],
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
) -> Literal["blind_expand_validation", "finish_attempt"]:
    return "finish_attempt" if state["failure_reason"] is not None else "blind_expand_validation"


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
        partial(draft_expansion, generator=generator, execution=execution),
    )
    graph.add_node(
        "blind_expand_validation",
        partial(blind_expand_validation, validator=validator, execution=execution, ranges=ranges),
    )
    graph.add_node("finish_attempt", finish_attempt)
    graph.add_edge(START, "initialize_attempt")
    graph.add_edge("initialize_attempt", "draft_expansion")
    graph.add_conditional_edges("draft_expansion", after_expansion_draft)
    graph.add_edge("blind_expand_validation", "finish_attempt")
    graph.add_edge("finish_attempt", END)
    return graph.compile()
