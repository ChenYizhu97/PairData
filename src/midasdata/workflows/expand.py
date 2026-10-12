"""One-level subgraph + three Sends; supersteps join all tasks before collect."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from midasdata.llm import ModelCaller
from midasdata.schemas import ExpandInput, ExpandState, ExpandUpdate, LevelState
from midasdata.workflows.steps import ExpandSteps


def build_level(steps: ExpandSteps):
    graph = StateGraph(LevelState)
    graph.add_node("llm_generator", steps.generate_level)
    graph.add_node("llm_validator2", steps.validate_level)
    graph.add_edge(START, "llm_generator")
    graph.add_conditional_edges(
        "llm_generator", steps.after_level_generate, [END, "llm_validator2"]
    )
    graph.add_edge("llm_validator2", END)
    return graph.compile()


def build_expand(caller: ModelCaller):
    steps = ExpandSteps(caller)
    subgraph = build_level(steps)

    async def single_level(state: LevelState) -> ExpandUpdate:
        result = await subgraph.ainvoke(state)
        return {"levels": {state["level"]: result["result"]}}

    async def sends(state: ExpandState) -> list[Send]:
        # First round: exactly three independent Sends, one per level.
        return [Send("single_level", steps.level_task(state, level)) for level in state["pending"]]

    graph = StateGraph(ExpandState, input_schema=ExpandInput)
    graph.add_node("initialize", steps.initialize)
    graph.add_node("dispatch", steps.dispatch)
    graph.add_node("single_level", single_level)
    graph.add_node("collect", steps.collect)
    graph.add_node("level_consistency_check", steps.validate_group)
    graph.add_node("assemble_result", steps.assemble_result)
    graph.add_node("reject", steps.reject)
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "dispatch")
    graph.add_conditional_edges("dispatch", sends, ["single_level"])
    graph.add_edge("single_level", "collect")
    graph.add_conditional_edges(
        "collect", steps.after_collect, ["dispatch", "level_consistency_check", "reject"]
    )
    graph.add_conditional_edges(
        "level_consistency_check", steps.after_group, ["assemble_result", "dispatch", "reject"]
    )
    graph.add_edge("assemble_result", END)
    graph.add_edge("reject", END)
    return graph.compile()
