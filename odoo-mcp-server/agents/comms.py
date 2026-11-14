"""Corporate Communications specialist node for searching, reading, and sending emails."""

from typing import Any

from agents.state import AgentState
from agents.worker_base import run_react_worker

COMMS_ALLOWED_TOOLS: set[str] = {
    "search_emails",
    "read_email",
    "send_email",
}

COMMS_SYSTEM_PROMPT: str = (
    "You are the Corporate Communications Specialist. Handle searching, reading, "
    "and sending emails via Gmail. If local PDF artifacts are listed in context, "
    "attach the exact file path when emailing invoices or reports."
)


async def comms_node(state: AgentState, deps: dict[str, Any]) -> dict[str, Any]:
    """Execute the Corporate Communications specialist node."""
    return await run_react_worker(
        agent_name="comms",
        system_prompt=COMMS_SYSTEM_PROMPT,
        allowed_tools=COMMS_ALLOWED_TOOLS,
        state=state,
        deps=deps,
    )
