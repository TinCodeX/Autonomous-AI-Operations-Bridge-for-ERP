"""Shared specialist execution engine for domain-scoped ReAct worker nodes."""

import base64
import io
import json
import os
import re
from types import SimpleNamespace
from typing import Any

from groq import AsyncGroq, BadRequestError
from langchain_core.messages import AIMessage
from mcp_utils import execute_mcp_tool, mcp_tools_to_groq_tools
from telegram.error import TelegramError

from agents.state import AgentState

LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

SENSITIVE_TOOLS: set[str] = {
    "create_customer",
    "add_product_to_stock",
    "create_quotation",
    "confirm_sale_order",
    "create_invoice_for_order",
    "validate_delivery_order",
    "send_email",
}


def filter_tools_for_allowlist(
    allowed_tools: set[str], all_mcp_tools: list[Any]
) -> list[dict[str, Any]]:
    """Filter MCP tools by allowed tool names and convert to Groq tool schemas."""
    scoped = [tool for tool in all_mcp_tools if tool.name in allowed_tools]
    return mcp_tools_to_groq_tools(SimpleNamespace(tools=scoped))


def _extract_pdf_paths(text: str) -> list[str]:
    """Find any '/tmp/*.pdf' paths mentioned in tool output."""
    return re.findall(r"/tmp/[\w.\-]+\.pdf", text)


async def _handle_pdf_delivery(bot: Any, chat_id: int, result_text: str) -> str:
    """Decode SUCCESS_PDF output, send document to Telegram, and return /tmp path."""
    try:
        parts = result_text.split(":", 2)
        filename = f"{parts[1]}.pdf"
        pdf_bytes = base64.b64decode(parts[2])
        if bot is not None:
            await bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(pdf_bytes),
                filename=filename,
                caption=f"📄 Generated Report: {filename}",
            )
        return (
            f"Report '{filename}' generated and saved at '/tmp/{filename}'. "
            f"Use attachment_path='/tmp/{filename}' if emailing it."
        )
    except (TelegramError, ValueError, IndexError, OSError) as exc:
        return f"PDF generated, but Telegram delivery failed: {exc!s}"


async def _execute_with_hitl(
    agent_name: str,
    func_name: str,
    func_args: dict[str, Any],
    state: AgentState,
    deps: dict[str, Any],
) -> str:
    """Enforce HITL approval for sensitive tools and handle PDF outputs."""
    session = deps["tool_sessions"].get(func_name)
    if session is None:
        return f"Error: No MCP session registered for '{func_name}'."

    approval_cb = deps.get("request_approval")
    if func_name in SENSITIVE_TOOLS and approval_cb is not None:
        approved = await approval_cb(
            chat_id=state["chat_id"],
            agent_name=agent_name,
            tool_name=func_name,
            arguments=func_args,
        )
        if not approved:
            return (
                f"Action '{func_name}' was REJECTED by the operator. "
                "Do not retry this action."
            )

    result_text = await execute_mcp_tool(session, func_name, func_args)
    if result_text.startswith("SUCCESS_PDF:"):
        return await _handle_pdf_delivery(
            deps.get("bot"), state["chat_id"], result_text
        )
    return result_text


async def _call_llm_with_retry(
    groq_client: AsyncGroq,
    messages: list[dict[str, Any]],
    scoped_tools: list[dict[str, Any]],
) -> Any:
    """Call Groq chat completion with a 3-attempt retry guard for output_parse_failed."""
    for attempt in range(3):
        try:
            return await groq_client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                tools=scoped_tools if scoped_tools else None,
                tool_choice="auto" if scoped_tools else None,
                parallel_tool_calls=False if scoped_tools else None,
                temperature=0.0,
            )
        except BadRequestError as exc:
            if "output_parse_failed" in str(exc) and attempt < 2:
                continue
            raise


async def _execute_turn_tools(
    agent_name: str,
    tool_calls: list[Any],
    state: AgentState,
    deps: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Execute requested tool calls and collect tool messages and artifacts."""
    tool_messages: list[dict[str, Any]] = []
    artifacts: list[str] = []
    for tc in tool_calls:
        func_name = tc.function.name
        func_args = json.loads(tc.function.arguments or "{}")
        tool_output = await _execute_with_hitl(
            agent_name, func_name, func_args, state, deps
        )
        artifacts.extend(_extract_pdf_paths(tool_output))
        tool_messages.append(
            {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_output,
            }
        )
    return tool_messages, artifacts


def _build_worker_messages(
    system_prompt: str, state: AgentState
) -> list[dict[str, Any]]:
    """Build the initial system and user prompt for a specialist turn."""
    artifacts = state.get("artifacts") or []
    artifacts_note = (
        f"Available local file artifacts: {', '.join(artifacts)}"
        if artifacts
        else "No local file artifacts generated yet."
    )
    task = state.get("task_instruction") or state["messages"][-1].content
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"Assigned Task: {task}\nContext: {artifacts_note}",
        },
    ]


async def run_react_worker(
    agent_name: str,
    system_prompt: str,
    allowed_tools: set[str],
    state: AgentState,
    deps: dict[str, Any],
) -> dict[str, Any]:
    """Execute a domain-scoped ReAct loop for a single specialist worker node."""
    groq_client: AsyncGroq = deps["groq_client"]
    scoped_tools = filter_tools_for_allowlist(allowed_tools, deps["all_mcp_tools"])
    messages = _build_worker_messages(system_prompt, state)
    discovered_artifacts: list[str] = []

    for _ in range(6):
        response = await _call_llm_with_retry(groq_client, messages, scoped_tools)
        msg = response.choices[0].message

        if not msg.tool_calls:
            final_content = msg.content or "Task completed."
            return {
                "messages": [
                    AIMessage(content=f"[{agent_name.upper()}] {final_content}")
                ],
                "artifacts": discovered_artifacts,
            }

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        tool_msgs, new_artifacts = await _execute_turn_tools(
            agent_name, msg.tool_calls, state, deps
        )
        messages.extend(tool_msgs)
        discovered_artifacts.extend(new_artifacts)

    return {
        "messages": [
            AIMessage(
                content=f"[{agent_name.upper()}] Reached step limit while executing task."
            )
        ],
        "artifacts": discovered_artifacts,
    }
