"""三档扩写：准备本轮 → 并行扩写 → 评估本轮 → 组校验 → 接受 / 修订 / 拒绝。

Expand three levels: prepare round → expand in parallel → evaluate → validate set → accept / revise / reject.
"""

from functools import partial
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from midasdata.config import ExpansionRanges
from midasdata.execution import ExecutionContext, ExecutionFailed, execute, operation_metadata
from midasdata.limits import MAX_DRAFTS, MAX_REQUEST_ATTEMPTS
from midasdata.model import LLMModel
from midasdata.prompts import messages
from midasdata.schemas import (
    EXPANSION_LEVELS,
    ExpandedPromptRecord,
    ExpansionLevel,
    ExpansionResult,
    ExpansionSetValidation,
    PromptExpansionDetails,
    QualityDecision,
    TaskResult,
)
from midasdata.validation import assess_expansion_set, check_expansion_set_contract
from midasdata.workflows.expansion_attempt import ExpansionAttemptInput, build_expansion_attempt


def merge_expansions(
    previous: dict[ExpansionLevel, ExpansionResult],
    updates: dict[ExpansionLevel, ExpansionResult],
) -> dict[ExpansionLevel, ExpansionResult]:
    """并行分支各更新一个档位；旧轮次不能覆盖新轮次的结果。

    Each parallel branch updates one level; older rounds cannot overwrite newer results.
    """
    merged = dict(previous)
    for level, result in updates.items():
        if level not in merged or result.round >= merged[level].round:
            merged[level] = result
    return merged


class PromptExpansionInput(TypedDict):
    task_id: str
    source_prompt: str


class PromptExpansionState(PromptExpansionInput, total=False):
    round_number: int
    pending_levels: list[ExpansionLevel]
    expansions: Annotated[dict[ExpansionLevel, ExpansionResult], merge_expansions]
    feedback_by_level: dict[ExpansionLevel, str]
    set_validation: ExpansionSetValidation | None
    set_decision: QualityDecision | None
    failure_reason: str | None
    result: TaskResult


class PromptExpansionOutput(TypedDict):
    result: TaskResult


# 节点 / Nodes
def initialize_expansion(state: PromptExpansionState) -> dict:
    return {
        "round_number": 0,
        "pending_levels": list(EXPANSION_LEVELS),
        "expansions": {},
        "feedback_by_level": {},
        "set_validation": None,
        "set_decision": None,
        "failure_reason": None,
    }


def prepare_round(state: PromptExpansionState) -> dict:
    return {
        "round_number": state["round_number"] + 1,
        "set_validation": None,
        "set_decision": None,
    }


async def expand_prompt(state: ExpansionAttemptInput, *, attempt_workflow) -> dict:
    completed = await attempt_workflow.ainvoke(state)
    return {"expansions": {state["level"]: completed["result"]}}


def evaluate_round(state: PromptExpansionState) -> dict:
    current = [state["expansions"][level] for level in state["pending_levels"]]
    if any(result.round != state["round_number"] for result in current):
        raise RuntimeError("round evaluation requires all dispatched attempts to complete")
    for result in current:
        if result.status == "technical_failed":
            return {"failure_reason": result.reason}
        if result.decision and result.decision.insufficient_expansion_space:
            return {"failure_reason": "insufficient_expansion_space"}
    retry_levels = [result.level for result in current if result.status != "passed"]
    if any(state["expansions"][level].generation_attempt >= MAX_DRAFTS for level in retry_levels):
        return {"failure_reason": "quality_budget_exhausted"}
    feedback = dict(state["feedback_by_level"])
    for level in retry_levels:
        result = state["expansions"][level]
        assert result.validation is not None
        feedback[level] = "\n".join([result.validation.feedback, result.reason])
    return {"pending_levels": retry_levels, "feedback_by_level": feedback}


def parse_set_report(
    output: dict,
    *,
    validator: LLMModel,
    source_prompt: str,
    expanded_prompts: dict[ExpansionLevel, str],
    ranges: ExpansionRanges,
) -> ExpansionSetValidation:
    """解析报告并检查修订契约，质量结果由校验节点随后计算。

    Parse the report and check its revision contract; the validation node then assesses quality.
    """
    report = validator.parse(output, ExpansionSetValidation)
    check_expansion_set_contract(report, source_prompt, expanded_prompts, ranges)
    return report


async def validate_expansion_set(
    state: PromptExpansionState,
    *,
    validator: LLMModel,
    execution: ExecutionContext,
    ranges: ExpansionRanges,
) -> dict:
    prompts = {level: state["expansions"][level].prompt for level in EXPANSION_LEVELS}
    payload = {
        "source": state["source_prompt"],
        "outputs": prompts,
        "piu_ranges": ranges.model_dump(),
    }
    metadata = operation_metadata(
        state["task_id"], "validate_group", model=validator.name, round_number=state["round_number"]
    )
    try:
        report = await execute(
            partial(
                validator.invoke,
                messages("validate_group", payload, ExpansionSetValidation),
                ExpansionSetValidation,
            ),
            context=execution,
            metadata=metadata,
            payload=payload,
            parse=partial(
                parse_set_report,
                validator=validator,
                source_prompt=state["source_prompt"],
                expanded_prompts=prompts,
                ranges=ranges,
            ),
            retry_if=validator.is_retryable,
            describe_error=validator.error_text,
            max_attempts=MAX_REQUEST_ATTEMPTS,
        )
    except ExecutionFailed as error:
        return {"failure_reason": f"technical_failure:{error}"}
    decision = assess_expansion_set(report, state["source_prompt"], prompts, ranges)
    execution.store.record_decision(metadata, decision)
    update = {
        "set_validation": report,
        "set_decision": decision,
        "pending_levels": [level for level in EXPANSION_LEVELS if level in decision.retry_levels],
    }
    if decision.insufficient_expansion_space:
        return {**update, "failure_reason": "insufficient_expansion_space"}
    if any(
        state["expansions"][level].generation_attempt >= MAX_DRAFTS
        for level in decision.retry_levels
    ):
        return {**update, "failure_reason": "group_quality_budget_exhausted"}
    feedback = dict(state["feedback_by_level"])
    for level in decision.retry_levels:
        feedback[level] = "\n".join(
            [report.feedback, report.levels[level].feedback, "; ".join(decision.reasons)]
        )
    return {**update, "feedback_by_level": feedback}


def expansion_result(state: PromptExpansionState, passed: bool) -> TaskResult:
    return TaskResult(
        id=state["task_id"],
        workflow="expand",
        status="passed" if passed else "rejected",
        source={"id": state["task_id"], "prompt": state["source_prompt"]},
        records=[
            ExpandedPromptRecord(
                id=state["task_id"], level=level, prompt=state["expansions"][level].prompt
            )
            for level in EXPANSION_LEVELS
        ]
        if passed
        else [],
        reason="" if passed else state["failure_reason"],
        details=PromptExpansionDetails(
            round=state["round_number"],
            levels=state["expansions"],
            group_validation=state["set_validation"],
            group_decision=state["set_decision"],
            feedback=state["feedback_by_level"],
        ),
    )


def accept_expansion(state: PromptExpansionState) -> dict:
    return {"result": expansion_result(state, True)}


def reject_expansion(state: PromptExpansionState) -> dict:
    return {"result": expansion_result(state, False)}


# 路由：分派的尝试没有自己的修订循环，所有轮次和预算由父图统一管理。
# Routing: the parent graph owns all revision rounds and budgets for dispatched attempts.
def dispatch_attempts(state: PromptExpansionState) -> list[Send]:
    tasks = []
    for level in state["pending_levels"]:
        previous = state["expansions"].get(level)
        task: ExpansionAttemptInput = {
            "task_id": state["task_id"],
            "source_prompt": state["source_prompt"],
            "level": level,
            "round_number": state["round_number"],
            "draft_number": previous.generation_attempt + 1 if previous else 1,
            "previous_prompt": previous.prompt if previous else "",
            "revision_feedback": state["feedback_by_level"].get(level, ""),
        }
        tasks.append(Send("expand_prompt", task))
    return tasks


def after_round(
    state: PromptExpansionState,
) -> Literal["prepare_round", "validate_expansion_set", "reject_expansion"]:
    if state["failure_reason"] is not None:
        return "reject_expansion"
    return "prepare_round" if state["pending_levels"] else "validate_expansion_set"


def after_set_validation(
    state: PromptExpansionState,
) -> Literal["accept_expansion", "prepare_round", "reject_expansion"]:
    if state["failure_reason"] is not None:
        return "reject_expansion"
    decision = state["set_decision"]
    assert decision is not None, "set routing requires current validation"
    return "accept_expansion" if decision.passed else "prepare_round"


# 构建工作流 / Build workflow
def build_prompt_expansion(
    generator: LLMModel, validator: LLMModel, execution: ExecutionContext, ranges: ExpansionRanges
):
    attempt_workflow = build_expansion_attempt(generator, validator, execution, ranges)
    graph = StateGraph(
        PromptExpansionState,
        input_schema=PromptExpansionInput,
        output_schema=PromptExpansionOutput,
    )
    graph.add_node("initialize_expansion", initialize_expansion)
    graph.add_node("prepare_round", prepare_round)
    graph.add_node("expand_prompt", partial(expand_prompt, attempt_workflow=attempt_workflow))
    graph.add_node("evaluate_round", evaluate_round)
    graph.add_node(
        "validate_expansion_set",
        partial(validate_expansion_set, validator=validator, execution=execution, ranges=ranges),
    )
    graph.add_node("accept_expansion", accept_expansion)
    graph.add_node("reject_expansion", reject_expansion)
    graph.add_edge(START, "initialize_expansion")
    graph.add_edge("initialize_expansion", "prepare_round")
    graph.add_conditional_edges("prepare_round", dispatch_attempts, ["expand_prompt"])
    graph.add_edge("expand_prompt", "evaluate_round")
    graph.add_conditional_edges("evaluate_round", after_round)
    graph.add_conditional_edges("validate_expansion_set", after_set_validation)
    graph.add_edge("accept_expansion", END)
    graph.add_edge("reject_expansion", END)
    return graph.compile()
