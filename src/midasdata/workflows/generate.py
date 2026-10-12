"""Initialize → generator → validator → assemble/reject; sampling precedes the graph."""

from langgraph.graph import END, START, StateGraph

from midasdata.llm import ModelCaller
from midasdata.schemas import GenerateState
from midasdata.workflows.steps import GenerateSteps


def build_generate(caller: ModelCaller, sampler: dict):
    steps = GenerateSteps(caller, sampler)
    graph = StateGraph(GenerateState)
    graph.add_node("initialize", steps.initialize)
    graph.add_node("llm_generator", steps.generate)
    graph.add_node("llm_validator1", steps.validate)
    graph.add_node("assemble_result", steps.assemble_result)
    graph.add_node("reject", steps.reject)
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "llm_generator")
    graph.add_conditional_edges(
        "llm_generator",
        steps.after_generate,
        ["reject", "llm_validator1"],
    )
    graph.add_conditional_edges(
        "llm_validator1", steps.route, ["assemble_result", "llm_generator", "reject"]
    )
    graph.add_edge("assemble_result", END)
    graph.add_edge("reject", END)
    return graph.compile()
