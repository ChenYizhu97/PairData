"""三档扩写：抽取目标 → 准备本轮 → 并行扩写 → 评估 → 组检查 → 接受 / 重生成 / 拒绝。

Expand three levels: sample targets → prepare round → expand → evaluate → check group → accept / regenerate / reject.
"""

import random
from functools import partial
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from midasdata.config import ExpansionRanges
from midasdata.execution import ExecutionContext, operation_metadata
from midasdata.limits import MAX_DRAFTS
from midasdata.model import LLMModel
from midasdata.schemas import (
    EXPANSION_LEVELS,
    ExpandedPromptRecord,
    ExpansionLevel,
    ExpansionResult,
    PromptExpansionDetails,
    QualityDecision,
    TaskResult,
)
from midasdata.storage import content_digest
from midasdata.validation import assess_group
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
    piu_targets: dict[ExpansionLevel, int]
    round_number: int
    pending_levels: list[ExpansionLevel]
    expansions: Annotated[dict[ExpansionLevel, ExpansionResult], merge_expansions]
    group_decision: QualityDecision | None
    failure_reason: str | None
    result: TaskResult


class PromptExpansionOutput(TypedDict):
    result: TaskResult


# 节点 / Nodes
def initialize_expansion(
    state: PromptExpansionState, *, execution: ExecutionContext, ranges: ExpansionRanges
) -> dict:
    # 每个档位只抽取一次目标；固定种子让断点恢复得到相同数量。
    # Sample one target per level; stable seeds reproduce the same counts on resume.
    targets = {}
    for level in EXPANSION_LEVELS:
        interval = getattr(ranges, level)
        seed = content_digest([execution.store.manifest["run_id"], state["task_id"], level])
        targets[level] = random.Random(seed).randint(interval.min, interval.max)
    return {
        "piu_targets": targets,
        "round_number": 0,
        "pending_levels": list(EXPANSION_LEVELS),
        "expansions": {},
        "group_decision": None,
        "failure_reason": None,
    }


def prepare_round(state: PromptExpansionState) -> dict:
    return {"round_number": state["round_number"] + 1}


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
    retry_levels = [result.level for result in current if result.status != "passed"]
    if any(state["expansions"][level].generation_attempt >= MAX_DRAFTS for level in retry_levels):
        return {"failure_reason": "quality_budget_exhausted"}
    return {"pending_levels": retry_levels}


def group_check(state: PromptExpansionState, *, execution: ExecutionContext) -> dict:
    """三档均通过后完成程序检查，并记录整组决策。

    Check the three accepted levels in code and record the group decision.
    """
    decision = assess_group(state["expansions"])
    metadata = operation_metadata(
        state["task_id"], "group_check", round_number=state["round_number"]
    )
    execution.store.record_decision(metadata, decision)
    return {
        "group_decision": decision,
        "failure_reason": None if decision.passed else "; ".join(decision.reasons),
    }


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
            group_decision=state["group_decision"],
        ),
    )


def accept_expansion(state: PromptExpansionState) -> dict:
    return {"result": expansion_result(state, True)}


def reject_expansion(state: PromptExpansionState) -> dict:
    return {"result": expansion_result(state, False)}


# 路由：父图统一管理轮次与预算，每次生成使用该档位固定的目标数量。
# Routing: the parent owns rounds and budgets; every generation uses its level's fixed target.
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
            "target_added_pius": state["piu_targets"][level],
        }
        tasks.append(Send("expand_prompt", task))
    return tasks


def after_round(
    state: PromptExpansionState,
) -> Literal["prepare_round", "group_check", "reject_expansion"]:
    if state["failure_reason"] is not None:
        return "reject_expansion"
    return "prepare_round" if state["pending_levels"] else "group_check"


def after_group_check(
    state: PromptExpansionState,
) -> Literal["accept_expansion", "reject_expansion"]:
    decision = state["group_decision"]
    assert decision is not None, "group routing requires a current decision"
    return "accept_expansion" if decision.passed else "reject_expansion"


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
    graph.add_node(
        "initialize_expansion", partial(initialize_expansion, execution=execution, ranges=ranges)
    )
    graph.add_node("prepare_round", prepare_round)
    graph.add_node("expand_prompt", partial(expand_prompt, attempt_workflow=attempt_workflow))
    graph.add_node("evaluate_round", evaluate_round)
    graph.add_node(
        "group_check",
        partial(group_check, execution=execution),
    )
    graph.add_node("accept_expansion", accept_expansion)
    graph.add_node("reject_expansion", reject_expansion)
    graph.add_edge(START, "initialize_expansion")
    graph.add_edge("initialize_expansion", "prepare_round")
    graph.add_conditional_edges("prepare_round", dispatch_attempts, ["expand_prompt"])
    graph.add_edge("expand_prompt", "evaluate_round")
    graph.add_conditional_edges("evaluate_round", after_round)
    graph.add_conditional_edges("group_check", after_group_check)
    graph.add_edge("accept_expansion", END)
    graph.add_edge("reject_expansion", END)
    return graph.compile()
