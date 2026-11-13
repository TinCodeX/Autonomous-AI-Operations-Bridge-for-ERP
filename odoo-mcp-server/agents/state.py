"""LangGraph shared state schema for the multi-agent ERP & Comms workflow."""

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

RouteTarget = Literal["sales", "inventory", "comms", "FINISH"]


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """Append newly generated file paths (e.g., '/tmp/INV_1.pdf') without duplicates."""
    merged = list(existing or [])
    for path in new or []:
        if path and path not in merged:
            merged.append(path)
    return merged


class AgentState(TypedDict):
    """Shared state flowing through the Supervisor and specialist worker nodes."""

    messages: Annotated[list[BaseMessage], add_messages]
    next_agent: RouteTarget
    task_instruction: str
    artifacts: Annotated[list[str], merge_artifacts]
    user_id: int
    chat_id: int
