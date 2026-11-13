"""LangGraph StateGraph compilation for the Supervisor-Worker architecture."""

from functools import partial
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agents.nodes import run_specialist_worker
from agents.state import AgentState
from agents.supervisor import supervisor_node


def route_from_supervisor(state: AgentState) -> str:
    """Conditional edge selector reading next_agent set by supervisor_node."""
    if state.get("next_agent") == "FINISH":
        return END
    return state["next_agent"]


def build_multi_agent_graph(runtime: dict[str, Any]):
    """Compile the Supervisor + Specialist Workers StateGraph with memory."""
    workflow = StateGraph(AgentState)

    workflow.add_node(
        "supervisor",
        partial(supervisor_node, groq_client=runtime["groq_client"]),
    )
    workflow.add_node(
        "sales",
        partial(run_specialist_worker, "sales", runtime=runtime),
    )
    workflow.add_node(
        "inventory",
        partial(run_specialist_worker, "inventory", runtime=runtime),
    )
    workflow.add_node(
        "comms",
        partial(run_specialist_worker, "comms", runtime=runtime),
    )

    workflow.add_edge(START, "supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {
            "sales": "sales",
            "inventory": "inventory",
            "comms": "comms",
            END: END,
        },
    )

    # Every specialist reports back to the Supervisor after finishing its step
    workflow.add_edge("sales", "supervisor")
    workflow.add_edge("inventory", "supervisor")
    workflow.add_edge("comms", "supervisor")

    checkpointer = MemorySaver()
    return workflow.compile(checkpointer=checkpointer)
