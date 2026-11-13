"""Telegram Bot Interface for Multi-Agent LangGraph ERP & Comms Orchestrator."""

import asyncio
import json
import os
import sys
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from groq import AsyncGroq, GroqError
from langchain_core.messages import HumanMessage
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from agents.graph import build_multi_agent_graph
from mcp_utils import MCP_TOOL_ERRORS, execute_mcp_tool

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not found in .env file.")

ALLOWED_USERS_RAW = os.getenv("ALLOWED_TELEGRAM_USERS", "")
ALLOWED_USERS = {
    int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip().isdigit()
}

CHECK_STOCK_INTERVAL_SECONDS = int(os.getenv("STOCK_CHECK_INTERVAL", "1800"))
client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

AGENT_BADGES: dict[str, str] = {
    "sales": "💼 Sales Agent",
    "inventory": "📦 Inventory Agent",
    "comms": "✉️ Comms Agent",
}


@dataclass
class BotRuntimeState:
    """Runtime state shared across Telegram handlers and LangGraph nodes."""

    tool_sessions: dict[str, ClientSession] = field(default_factory=dict)
    odoo_session: ClientSession | None = None
    all_mcp_tools: list[Any] = field(default_factory=list)
    pending_approvals: dict[str, dict] = field(default_factory=dict)
    active_chats: set[int] = field(default_factory=set)
    graph: Any = None


STATE = BotRuntimeState()


def is_authorized(user_id: int) -> bool:
    """Check if the requesting Telegram user is in the whitelist."""
    return bool(ALLOWED_USERS) and user_id in ALLOWED_USERS


async def send_safe_message(chat: Any, text: str) -> None:
    """Send with Markdown formatting; fallback to plain text if parsing fails."""
    try:
        await chat.send_message(text, parse_mode="Markdown")
    except TelegramError:
        await chat.send_message(text)


async def check_stock_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Manual on-demand check for low-inventory items."""
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    if not STATE.odoo_session:
        await update.message.reply_text("⚠️ Odoo MCP server session is not ready.")
        return

    await update.message.reply_text(
        "🔍 Scanning warehouse stock levels against threshold..."
    )
    report = await execute_mcp_tool(
        STATE.odoo_session, "get_low_stock_products", {"threshold": 5.0}
    )
    try:
        await send_safe_message(update.effective_chat, report)
    except TelegramError as exc:
        await update.message.reply_text(f"Stock scan failed: {exc!s}")


async def stock_monitor_worker(bot: Any) -> None:
    """Periodically audit inventory and notify active authorized Telegram chats."""
    print(
        "[+] Autonomous stock monitor worker started "
        f"(interval: {CHECK_STOCK_INTERVAL_SECONDS}s)."
    )

    while True:
        try:
            await asyncio.sleep(CHECK_STOCK_INTERVAL_SECONDS)
            if not STATE.odoo_session:
                continue

            report = await execute_mcp_tool(
                STATE.odoo_session, "get_low_stock_products", {"threshold": 5.0}
            )
            if "Low Stock Alert" in report:
                for chat_id in list(STATE.active_chats):
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"🔔 [Autonomous Operations Alert]\n\n{report}",
                        )
                    except TelegramError as err:
                        print(f"[!] Failed to dispatch alert to {chat_id}: {err}")

        except asyncio.CancelledError:
            print("[-] Stock monitor worker cancelled.")
            break
        except (*MCP_TOOL_ERRORS, TelegramError) as exc:
            print(f"[!] Error in stock_monitor_worker: {exc}")
            await asyncio.sleep(30)


async def start_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command and register active chat."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not is_authorized(user.id):
        print(
            f"[!] Unauthorized /start attempt by user ID: {user.id} (@{user.username})"
        )
        await update.message.reply_text(
            f"⛔ Unauthorized access. Your Telegram User ID is `{user.id}`.\n"
            "Add this ID to `ALLOWED_TELEGRAM_USERS` in your `.env` to gain access.",
            parse_mode="Markdown",
        )
        return

    STATE.active_chats.add(chat_id)
    welcome_text = (
        "🧠 *Multi-Agent ERP & Comms Orchestrator Connected (v2.0)*\n\n"
        "Active Specialists:\n"
        "• 💼 *Sales Agent* — Customers, Quotations, Orders & PDF Invoices\n"
        "• 📦 *Inventory Agent* — Stock Levels, Audits & Deliveries\n"
        "• ✉️ *Comms Agent* — Gmail Search, Reading & PDF Dispatch\n\n"
        "Try a cross-domain workflow:\n"
        "• _Check if Desk Chairs are in stock; if yes, create a quotation for "
        "Azure Interior for 2 chairs, invoice it, and email them the PDF._"
    )
    await send_safe_message(update.effective_chat, welcome_text)


async def handle_callback_query(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Process inline approve/reject button callbacks for sensitive tool calls."""
    query = update.callback_query
    if not query:
        return

    user = update.effective_user
    if not is_authorized(user.id):
        await query.answer("Unauthorized action.", show_alert=True)
        return

    try:
        await query.answer()
    except TelegramError as exc:
        print(f"[!] Warning on query.answer: {exc}")

    data = query.data or ""
    if not data.startswith(("approve:", "reject:")):
        return

    action, approval_id = data.split(":", 1)
    record = STATE.pending_approvals.get(approval_id)

    if not record:
        try:
            await query.edit_message_text(
                f"{query.message.text}\n\n⚠️ [SESSION EXPIRED / TIMED OUT]"
            )
        except TelegramError as exc:
            print(f"[!] Warning on expired message edit: {exc}")
        return

    is_approved = action == "approve"
    record["approved"] = is_approved
    record["event"].set()

    status_tag = (
        "✅ [AUTHORIZED BY OPERATOR]" if is_approved else "❌ [REJECTED BY OPERATOR]"
    )
    try:
        current_text = query.message.text or ""
        await query.edit_message_text(f"{current_text}\n\n{status_tag}")
    except TelegramError as exc:
        print(f"[!] Warning on edit_message_text: {exc}")


async def request_approval_callback(
    bot: Any,
    chat_id: int,
    agent_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    """Send inline HITL approval prompt from any specialist node and await response."""
    approval_id = str(uuid.uuid4())[:8]
    event = asyncio.Event()
    STATE.pending_approvals[approval_id] = {"event": event, "approved": False}

    badge = AGENT_BADGES.get(agent_name, agent_name.upper())
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{approval_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{approval_id}"),
        ]
    ]
    args_formatted = json.dumps(arguments, indent=2)

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"⚠️ Approval Required ({badge})\n\n"
            f"Tool: {tool_name}\nPayload:\n{args_formatted}"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    try:
        await asyncio.wait_for(event.wait(), timeout=600.0)
        return STATE.pending_approvals[approval_id]["approved"]
    except TimeoutError:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏱️ Timed out waiting for approval on '{tool_name}'. Action cancelled.",
        )
        return False
    finally:
        STATE.pending_approvals.pop(approval_id, None)


async def handle_user_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Stream incoming user requests through the LangGraph Supervisor-Worker graph."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not is_authorized(user.id):
        print(f"[!] Blocked message from unauthorized user ID: {user.id}")
        await update.message.reply_text(
            f"⛔ Unauthorized access. ID `{user.id}` is not permitted.",
            parse_mode="Markdown",
        )
        return

    STATE.active_chats.add(chat_id)
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    graph_input = {
        "messages": [HumanMessage(content=update.message.text)],
        "next_agent": "supervisor",
        "task_instruction": "",
        "artifacts": [],
        "user_id": user.id,
        "chat_id": chat_id,
    }
    config = {"configurable": {"thread_id": str(chat_id)}, "recursion_limit": 15}

    final_reply = "Workflow completed."
    try:
        async for event in STATE.graph.astream(
            graph_input, config=config, stream_mode="updates"
        ):
            for node_name, node_output in event.items():
                if node_name == "supervisor":
                    next_target = node_output.get("next_agent")
                    if next_target and next_target != "FINISH":
                        badge = AGENT_BADGES.get(next_target, next_target)
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"🧠 Supervisor delegating to *{badge}*...",
                            parse_mode="Markdown",
                        )
                    elif next_target == "FINISH" and node_output.get("messages"):
                        final_reply = node_output["messages"][-1].content
                elif node_name in AGENT_BADGES and node_output.get("messages"):
                    final_reply = node_output["messages"][-1].content

    except GroqError as exc:
        await update.message.reply_text(f"⚠️ AI Engine error: {exc!s}")
        return

    await send_safe_message(update.effective_chat, final_reply)


def _build_telegram_app() -> Application:
    """Create and configure the Telegram Application with all handlers."""
    request_config = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
    )

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .request(request_config)
        .get_updates_request(request_config)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("check_stock", check_stock_command))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message)
    )
    return app


async def _connect_mcp_servers(stack: AsyncExitStack) -> None:
    """Start Odoo and Gmail MCP servers over stdio and register their tools."""
    python_executable = sys.executable
    base_dir = os.path.dirname(__file__)
    mcp_servers = {
        "odoo": StdioServerParameters(
            command=python_executable,
            args=[os.path.join(base_dir, "server.py")],
            env=dict(os.environ),
        ),
        "gmail": StdioServerParameters(
            command=python_executable,
            args=[os.path.join(base_dir, "email_server.py")],
            env=dict(os.environ),
        ),
    }

    for server_name, params in mcp_servers.items():
        print(f"[+] Starting {server_name} MCP server over stdio...")
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(params)
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()

        if server_name == "odoo":
            STATE.odoo_session = session

        tools_data = await session.list_tools()
        for tool in tools_data.tools:
            STATE.tool_sessions[tool.name] = session
            STATE.all_mcp_tools.append(tool)

        print(f"[+] Loaded {len(tools_data.tools)} tools from {server_name} MCP.")

    print(f"[+] Total MCP tools registered for LangGraph: {len(STATE.all_mcp_tools)}.")


async def main() -> None:
    """Connect MCP servers, compile the LangGraph workflow, and start Telegram."""
    async with AsyncExitStack() as stack:
        await _connect_mcp_servers(stack)
        app = _build_telegram_app()

        runtime = {
            "groq_client": client,
            "tool_sessions": STATE.tool_sessions,
            "all_mcp_tools": STATE.all_mcp_tools,
            "bot": app.bot,
            "request_approval": lambda **kwargs: request_approval_callback(
                bot=app.bot, **kwargs
            ),
        }
        STATE.graph = build_multi_agent_graph(runtime)
        print("[+] LangGraph Supervisor-Worker workflow compiled.")

        print("[+] Initializing Telegram application...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        print("[+] Telegram polling started. Listening for messages...")

        monitor_task = asyncio.create_task(stock_monitor_worker(app.bot))

        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        finally:
            monitor_task.cancel()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[!] Telegram bot terminated.")
