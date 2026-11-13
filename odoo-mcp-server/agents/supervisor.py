"""Supervisor router node for multi-agent intent decomposition and delegation."""

import json
import os
from typing import Any

from groq import AsyncGroq
from langchain_core.messages import AIMessage

from agents.state import AgentState, RouteTarget

SUPERVISOR_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

SUPERVISOR_PROMPT = """You are the Enterprise Operations Supervisor orchestrating three specialized AI agents:
1. "inventory": Checks product stock levels, audits low stock, adds stock, and validates delivery orders.
2. "sales": Searches/creates customers, builds quotations, confirms sale orders, creates invoices, and generates invoice PDFs.
3. "comms": Searches, reads, and sends emails via Gmail (including attaching generated PDF files from /tmp).
4. "FINISH": Select when the user's request is completely fulfilled or cannot proceed further.

Rules:
- Delegate ONE concrete step at a time to the most appropriate specialist.
- Pass exact IDs, emails, quantities, and /tmp/*.pdf file paths in "task_instruction" so the specialist has everything it needs.
- Never repeat a step that already succeeded in the message history.
- When "FINISH" is chosen, write a clear, helpful final response for the user in "final_response".

Respond ONLY with valid JSON matching this schema:
{
  "next_agent": "sales" | "inventory" | "comms" | "FINISH",
  "task_instruction": "Specific instruction for the chosen specialist (empty if FINISH)",
  "final_response": "Final summary for the user if next_agent is FINISH (empty otherwise)"
}"""

VALID_TARGETS: set[str] = {"sales", "inventory", "comms", "FINISH"}


async def supervisor_node(state: AgentState, groq_client: AsyncGroq) -> dict[str, Any]:
    """Inspect current AgentState and route to the next specialist or FINISH."""
    history_lines: list[str] = []
    for msg in state["messages"]:
        role = "User" if msg.type == "human" else "Agent"
        history_lines.append(f"{role}: {msg.content}")

    artifacts = state.get("artifacts", [])
    artifacts_line = (
        f"Generated Artifacts: {', '.join(artifacts)}"
        if artifacts
        else "Generated Artifacts: None"
    )

    prompt_payload = "\n".join(history_lines) + f"\n\n{artifacts_line}"

    response = await groq_client.chat.completions.create(
        model=SUPERVISOR_MODEL,
        messages=[
            {"role": "system", "content": SUPERVISOR_PROMPT},
            {"role": "user", "content": prompt_payload},
        ],
        response_format={"type": "json_object"},
    )

    raw_content = response.choices[0].message.content or "{}"
    try:
        decision = json.loads(raw_content)
    except json.JSONDecodeError:
        decision = {
            "next_agent": "FINISH",
            "task_instruction": "",
            "final_response": raw_content,
        }

    target = decision.get("next_agent", "FINISH")
    next_agent: RouteTarget = target if target in VALID_TARGETS else "FINISH"

    if next_agent == "FINISH":
        final_text = (
            decision.get("final_response") or "All requested tasks are complete."
        )
        return {
            "next_agent": "FINISH",
            "task_instruction": "",
            "messages": [AIMessage(content=final_text)],
        }

    return {
        "next_agent": next_agent,
        "task_instruction": decision.get("task_instruction", ""),
    }
