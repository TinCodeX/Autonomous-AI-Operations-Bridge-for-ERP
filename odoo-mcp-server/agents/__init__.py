"""Multi-agent LangGraph architecture for Odoo ERP and Gmail operations."""

from agents.graph import build_multi_agent_graph
from agents.state import AgentState

__all__ = ["AgentState", "build_multi_agent_graph"]
