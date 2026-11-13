"""Specialized LangGraph worker nodes with domain-scoped MCP tools and HITL."""

import base64
import io
import json
import os
import re
from types import SimpleNamespace
from typing import Any

from groq import AsyncGroq
from langchain_core.messages import AIMessage
from telegram.error import TelegramError

from agents.state import AgentState
from mcp_utils import execute_mcp_tool, mcp_tools_to_groq_tools

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

AGENT_TOOL_ALLOWLIST: dict[str, set[str]] = {
    "sales": {
        "search_customers",
        "create_customer",
        "search_products",
        "get_recent_quotations",
        "create_quotation",
        "confirm_sale_order",
        "create_invoice_for_order",
        "get_invoice_pdf",
    },
    "inventory": {
        "search_products",
        "get_stock_level",
        "get_low_stock_products",
        "add_product_to_stock",
        "get_order_deliveries",
        "validate_delivery_order",
    },
    "comms": {
        "search_emails",
        "read_email",
        "send_email",
    },
}

SYSTEM_PROMPTS: dict[str, str] = {
    "sales": (
        "You are the Odoo Sales & Billing Specialist. Handle customer records, "
        "quotations, sale order confirmations, invoices, and PDF generation. "
        "Never invent IDs—look them up first. Return concise, factual results."
    ),
    "inventory": (
        "You are the Odoo Inventory & Fulfillment Specialist. Handle stock checks, "
        "low-stock audits, stock additions, and delivery order validations. "
        "Never invent product or picking IDs—look them up first."
    ),
    "comms": (
        "You are the Corporate Communications Specialist. Handle searching, reading, "
        "and sending emails via Gmail. If local PDF artifacts are listed in context, "
        "attach the exact file path when emailing invoices or reports."
    ),
}


def filter_tools_for_agent(
    agent_name: str, all_mcp_tools: list[Any]
) -> list[dict[str, Any]]:
    """Filter MCP tools by agent allowlist and convert to Groq tool schema."""
    allowed = AGENT_TOOL_ALLOWLIST.get(agent_name, set())
    scoped = [tool for tool in all_mcp_tools if tool.name in allowed]
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
    runtime: dict[str, Any],
) -> str:
    """Enforce HITL approval for sensitive tools and handle PDF outputs."""
    session = runtime["tool_sessions"].get(func_name)
    if session is None:
        return f"Error: No MCP session registered for '{func_name}'."

    approval_cb = runtime.get("request_approval")
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
            runtime.get("bot"), state["chat_id"], result_text
        )
    return result_text


async def run_specialist_worker(
    agent_name: str,
    state: AgentState,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Execute a domain-scoped ReAct loop for a single specialist worker node."""
    groq_client: AsyncGroq = runtime["groq_client"]
    scoped_tools = filter_tools_for_agent(agent_name, runtime["all_mcp_tools"])

    artifacts_note = (
        f"Available local file artifacts: {', '.join(state.get('artifacts', []))}"
        if state.get("artifacts")
        else "No local file artifacts generated yet."
    )
    task = state.get("task_instruction") or state["messages"][-1].content

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPTS[agent_name]},
        {
            "role": "user",
            "content": f"Assigned Task: {task}\nContext: {artifacts_note}",
        },
    ]

    discovered_artifacts: list[str] = []

    for _ in range(6):
        response = await groq_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            tools=scoped_tools if scoped_tools else None,
            tool_choice="auto" if scoped_tools else None,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            summary = msg.content or "Task completed."
            return {
                "messages": [AIMessage(content=f"[{agent_name.upper()}] {summary}")],
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

        for tc in msg.tool_calls:
            func_name = tc.function.name
            func_args = json.loads(tc.function.arguments or "{}")
            tool_output = await _execute_with_hitl(
                agent_name, func_name, func_args, state, runtime
            )
            discovered_artifacts.extend(_extract_pdf_paths(tool_output))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_output,
                }
            )

    return {
        "messages": [
            AIMessage(
                content=f"[{agent_name.upper()}] Reached step limit while executing task."
            )
        ],
        "artifacts": discovered_artifacts,
    }
