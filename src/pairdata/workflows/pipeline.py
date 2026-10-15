"""Fixed Source → Evaluate → Gate → Target → Evaluate → Gate workflow."""

from functools import partial
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from pairdata.bindings import lookup
from pairdata.execution import ExecutionContext, ExecutionFailed, execute, operation_metadata
from pairdata.recipe import Recipe
from pairdata.rules import evaluate_rule
from pairdata.sampler import sample_fields
from pairdata.schemas import StageResult, TaskResult


class PipelineInput(TypedDict):
    task_id: str
    index: int


class PipelineState(PipelineInput, total=False):
    source: StageResult
    target: StageResult | None
    failure_reason: str | None
    result: TaskResult


class PipelineOutput(TypedDict):
    result: TaskResult


async def initialize(state):
    return {"source": StageResult(), "target": None, "failure_reason": None}


async def sample_stage(state, *, stage, recipe, execution):
    fields = recipe.stages[stage].sampler.root
    sample = sample_fields(
        fields, execution.store.manifest["request"]["seed"], state["index"], stage
    )
    metadata = operation_metadata(state["task_id"], f"{stage}.sample")
    return {stage: StageResult(sample=execution.store.record_sample(metadata, sample))}


async def call_model(
    state, stage, operation, context, generation_attempt, *, recipe, models, execution
):
    call = getattr(recipe.stages[stage], operation)
    model = models[call.settings.model]
    inputs, messages = call.renderer.render(context, call.validator.schema)
    metadata = operation_metadata(
        state["task_id"],
        f"{stage}.{operation}",
        model=model.name,
        generation_attempt=generation_attempt,
    )
    return await execute(
        partial(model.invoke, messages, call.validator.schema),
        context=execution,
        metadata=metadata,
        payload={"inputs": inputs, "messages": messages, "schema": call.validator.schema},
        parse=call.validator.parse,
        retry_if=model.is_retryable,
        describe_error=model.error_text,
        max_attempts=recipe.settings.runtime.max_request_attempts,
    )


async def generate(state, *, stage, recipe, models, execution):
    previous = state[stage]
    assert previous is not None
    context = {
        "sample": previous.sample,
        "previous": previous.candidate,
        "feedback": {
            "evaluation": previous.evaluation,
            "decision": previous.decision.model_dump() if previous.decision else None,
        }
        if previous.generation_attempt
        else None,
    }
    if stage == "target":
        context["source"] = state["source"].candidate
    current = StageResult(
        sample=previous.sample, generation_attempt=previous.generation_attempt + 1
    )
    try:
        current.generation = await call_model(
            state,
            stage,
            "generate",
            context,
            current.generation_attempt,
            recipe=recipe,
            models=models,
            execution=execution,
        )
    except ExecutionFailed as error:
        current.status = "technical_failed"
        current.reason = str(error)
        return {stage: current, "failure_reason": f"{stage}.generate: {error}"}
    current.candidate = lookup(
        current.generation, recipe.stages[stage].settings.generate.output_path
    )
    return {stage: current}


def evaluation_context(state, stage):
    current = state[stage]
    context = {
        "sample": current.sample,
        "generation": current.generation,
        "candidate": current.candidate,
    }
    if stage == "target":
        context["source"] = state["source"].candidate
    return context


async def evaluate(state, *, stage, recipe, models, execution):
    current = state[stage].model_copy(deep=True)
    try:
        current.evaluation = await call_model(
            state,
            stage,
            "evaluate",
            evaluation_context(state, stage),
            current.generation_attempt,
            recipe=recipe,
            models=models,
            execution=execution,
        )
    except ExecutionFailed as error:
        current.status = "technical_failed"
        current.reason = str(error)
        return {stage: current, "failure_reason": f"{stage}.evaluate: {error}"}
    return {stage: current}


async def gate(state, *, stage, recipe, execution):
    current = state[stage].model_copy(deep=True)
    context = {**evaluation_context(state, stage), "evaluation": current.evaluation}
    current.decision = evaluate_rule(recipe.stages[stage].decision, context)
    metadata = operation_metadata(
        state["task_id"], f"{stage}.gate", generation_attempt=current.generation_attempt
    )
    execution.store.record_decision(metadata, current.decision)
    current.status = "passed" if current.decision.passed else "quality_failed"
    current.reason = "; ".join(current.decision.reasons)
    exhausted = (
        not current.decision.passed
        and current.generation_attempt >= recipe.stages[stage].settings.max_generations
    )
    return {
        stage: current,
        "failure_reason": f"{stage}.quality_budget_exhausted" if exhausted else None,
    }


async def after_call(state, *, next_node):
    return "reject" if state["failure_reason"] else next_node


async def after_gate(state, *, stage):
    if state["failure_reason"]:
        return "reject"
    if state[stage].status == "passed":
        return "target_sample" if stage == "source" else "accept"
    return f"{stage}_generate"


async def finish(state, *, passed):
    return {
        "result": TaskResult(
            id=state["task_id"],
            status="passed" if passed else "rejected",
            source=state["source"],
            target=state["target"],
            reason=state["failure_reason"] or "",
        )
    }


def build_pipeline(recipe: Recipe, models: dict, execution: ExecutionContext):
    # Async entry points keep state changes and store writes on this event loop.
    graph = StateGraph(PipelineState, input_schema=PipelineInput, output_schema=PipelineOutput)
    graph.add_node("initialize", initialize)
    for stage in ("source", "target"):
        shared = {"stage": stage, "recipe": recipe, "execution": execution}
        graph.add_node(f"{stage}_sample", partial(sample_stage, **shared))
        graph.add_node(f"{stage}_generate", partial(generate, models=models, **shared))
        graph.add_node(f"{stage}_evaluate", partial(evaluate, models=models, **shared))
        graph.add_node(f"{stage}_gate", partial(gate, **shared))
        graph.add_edge(f"{stage}_sample", f"{stage}_generate")
        for operation, following in (("generate", "evaluate"), ("evaluate", "gate")):
            destination = f"{stage}_{following}"
            graph.add_conditional_edges(
                f"{stage}_{operation}",
                partial(after_call, next_node=destination),
                [destination, "reject"],
            )
        success = "target_sample" if stage == "source" else "accept"
        graph.add_conditional_edges(
            f"{stage}_gate",
            partial(after_gate, stage=stage),
            [success, f"{stage}_generate", "reject"],
        )
    graph.add_node("accept", partial(finish, passed=True))
    graph.add_node("reject", partial(finish, passed=False))
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "source_sample")
    graph.add_edge("accept", END)
    graph.add_edge("reject", END)
    return graph.compile()
