"""Supervisor router node for multi-agent intent decomposition and loop prevention."""

import json
import os
import re
from typing import Any

from groq import AsyncGroq, BadRequestError
from langchain_core.messages import AIMessage, HumanMessage

from agents.state import AgentState, RouteTarget

SUPERVISOR_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
VALID_TARGETS: set[str] = {"sales", "inventory", "comms", "FINISH"}

SUPERVISOR_PROMPT = """You are the Enterprise Operations Supervisor orchestrating three specialized AI agents:
1. "inventory": Checks product stock levels, audits low stock, adds stock, and validates delivery orders.
2. "sales": Searches/creates customers, builds quotations, confirms sale orders, creates invoices, and generates invoice PDFs.
3. "comms": Searches, reads, and sends emails via Gmail (including attaching generated PDF files from /tmp).
4. "FINISH": Select when the user's request is completely fulfilled or cannot proceed further.

CRITICAL ANTI-LOOP RULES:
- Delegate ONE concrete step at a time to the most appropriate specialist.
- Pass exact IDs, emails, quantities, and /tmp/*.pdf file paths in "task_instruction".
- NEVER delegate to the same specialist twice in a row.
- If a specialist already ran and reported 0 stock, no matching records, or an error, set "next_agent" to "FINISH" and summarize that outcome in "final_response".

Respond ONLY with valid JSON matching this schema:
{
  "next_agent": "sales" | "inventory" | "comms" | "FINISH",
  "task_instruction": "Specific instruction for the chosen specialist (empty if FINISH)",
  "final_response": "Final summary for the user if next_agent is FINISH (empty otherwise)"
}"""


def _get_latest_turn_messages(messages: list[Any]) -> tuple[str, list[str]]:
    """Extract the latest HumanMessage and any subsequent AIMessage outputs."""
    last_human_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        if isinstance(messages[idx], HumanMessage):
            last_human_idx = idx
            break

    user_prompt = str(messages[last_human_idx].content) if last_human_idx >= 0 else ""
    slice_start = last_human_idx + 1 if last_human_idx >= 0 else 0
    outputs = [
        str(m.content)
        for m in messages[slice_start:]
        if isinstance(m, AIMessage) and m.content
    ]
    return user_prompt, outputs


def _parse_supervisor_json(raw_content: str) -> dict[str, str]:
    """Parse JSON decision from supervisor output safely."""
    cleaned = raw_content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL
        ).strip()
    data = json.loads(cleaned)
    target = str(data.get("next_agent", "FINISH")).strip()
    if target not in VALID_TARGETS:
        target = "FINISH"
    return {
        "next_agent": target,
        "task_instruction": str(data.get("task_instruction", "")).strip(),
        "final_response": str(data.get("final_response", "")).strip(),
    }


async def supervisor_node(state: AgentState, groq_client: AsyncGroq) -> dict[str, Any]:
    """Inspect current AgentState and route to the next specialist or FINISH."""
    messages = state.get("messages", [])
    user_prompt, worker_outputs = _get_latest_turn_messages(messages)
    hop_count = int(state.get("hop_count") or 0)
    last_agent = str(state.get("last_agent") or "")

    # Guardrail 1: Hard cap at 4 specialist hops per user turn
    if hop_count >= 4:
        final_text = (
            worker_outputs[-1] if worker_outputs else "Reached max workflow steps."
        )
        return {
            "next_agent": "FINISH",
            "task_instruction": "",
            "hop_count": 0,
            "last_agent": "",
            "messages": [AIMessage(content=final_text)],
        }

    artifacts = state.get("artifacts", [])
    artifacts_line = (
        f"Generated Artifacts: {', '.join(artifacts)}"
        if artifacts
        else "Generated Artifacts: None"
    )
    outputs_block = "\n".join(f"- {out}" for out in worker_outputs) or "None yet."

    prompt_payload = (
        f"Latest User Request:\n{user_prompt}\n\n"
        f"Specialist Outputs This Turn (hop {hop_count}/4, last_agent='{last_agent}'):\n"
        f"{outputs_block}\n\n"
        f"{artifacts_line}\n\n"
        "Return your routing decision as JSON."
    )

    decision: dict[str, str] | None = None
    for attempt in range(3):
        try:
            response = await groq_client.chat.completions.create(
                model=SUPERVISOR_MODEL,
                messages=[
                    {"role": "system", "content": SUPERVISOR_PROMPT},
                    {"role": "user", "content": prompt_payload},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            raw_content = response.choices[0].message.content or "{}"
            decision = _parse_supervisor_json(raw_content)
            break
        except (BadRequestError, json.JSONDecodeError) as exc:
            if attempt < 2:
                continue
            fallback = (
                worker_outputs[-1]
                if worker_outputs
                else f"Unable to route request cleanly: {exc!s}"
            )
            return {
                "next_agent": "FINISH",
                "task_instruction": "",
                "hop_count": 0,
                "last_agent": "",
                "messages": [AIMessage(content=fallback)],
            }

    assert decision is not None
    next_agent: RouteTarget = decision["next_agent"]  # type: ignore[assignment]

    # Guardrail 2: Never delegate to the exact same specialist twice in a row
    if last_agent and next_agent == last_agent:
        final_text = decision["final_response"] or (
            worker_outputs[-1] if worker_outputs else "Completed check."
        )
        return {
            "next_agent": "FINISH",
            "task_instruction": "",
            "hop_count": 0,
            "last_agent": "",
            "messages": [AIMessage(content=final_text)],
        }

    if next_agent == "FINISH":
        final_text = decision["final_response"] or (
            worker_outputs[-1]
            if worker_outputs
            else "All requested tasks are complete."
        )
        return {
            "next_agent": "FINISH",
            "task_instruction": "",
            "hop_count": 0,
            "last_agent": "",
            "messages": [AIMessage(content=final_text)],
        }

    return {
        "next_agent": next_agent,
        "task_instruction": decision["task_instruction"] or user_prompt,
        "hop_count": hop_count + 1,
        "last_agent": next_agent,
    }
