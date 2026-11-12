"""Telegram Bot Interface for Multi-Server MCP Agent (Odoo + Gmail) with Inline HITL."""

import asyncio
import base64
import io
import json
import os
import sys
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from dotenv import load_dotenv
from groq import AsyncGroq, GroqError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp_utils import MCP_TOOL_ERRORS, execute_mcp_tool, mcp_tools_to_groq_tools
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
MODEL_NAME = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
SENSITIVE_TOOLS = {
    "create_customer",
    "create_quotation",
    "add_product_to_stock",
    "confirm_sale_order",
    "create_invoice_for_order",
    "validate_delivery_order",
    "send_email",
}


@dataclass
class AgentState:
    """Runtime state shared across Telegram bot handlers."""

    tool_sessions: dict[str, ClientSession] = field(default_factory=dict)
    odoo_session: ClientSession | None = None
    groq_tools: list = field(default_factory=list)
    pending_approvals: dict[str, dict] = field(default_factory=dict)
    chat_histories: dict[int, list] = field(default_factory=dict)


STATE = AgentState()


def is_authorized(user_id: int) -> bool:
    """Check if the requesting Telegram user is in the whitelist."""
    if not ALLOWED_USERS:
        return False
    return user_id in ALLOWED_USERS


async def send_safe_message(chat, text: str):
    """Attempt sending with Markdown formatting; fallback to plain text if syntax fails."""
    try:
        await chat.send_message(text, parse_mode="Markdown")
    except TelegramError:
        await chat.send_message(text)


async def check_stock_command(update: Update, _context: ContextTypes.DEFAULT_TYPE):
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


async def stock_monitor_worker(bot):
    """Periodically audit inventory and notify authorized Telegram chats."""
    print(
        "[+] Autonomous stock monitor worker started "
        f"(interval: {CHECK_STOCK_INTERVAL_SECONDS}s)."
    )

    while True:
        try:
            await asyncio.sleep(CHECK_STOCK_INTERVAL_SECONDS)
            if not STATE.odoo_session:
                continue

            result = await STATE.odoo_session.call_tool(
                "get_low_stock_products", arguments={"threshold": 5.0}
            )
            report = "".join(
                [part.text for part in result.content if hasattr(part, "text")]
            )

            if "Low Stock Alert" in report:
                for chat_id in list(STATE.chat_histories.keys()):
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"🔔 [Autonomous Operations Alert]\n\n{report}",
                        )
                    except TelegramError as err:
                        print(
                            f"[!] Failed to dispatch proactive alert to {chat_id}: {err}"
                        )

        except asyncio.CancelledError:
            print("[-] Stock monitor worker cancelled.")
            break
        except (*MCP_TOOL_ERRORS, TelegramError) as exc:
            print(f"[!] Error in stock_monitor_worker: {exc}")
            await asyncio.sleep(30)


async def start_command(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command and initialize user chat history."""
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

    STATE.chat_histories[chat_id] = [
        {
            "role": "system",
            "content": (
                "You are an autonomous ERP & Communications Operations Assistant connected to "
                "Odoo 18 and Gmail. You have access to tools for customer management, multi-item "
                "quotations, invoicing, inventory tracking, and reading/sending emails. "
                "Always check product availability and partner IDs before creating quotations. "
                "When creating multi-item quotations, structure items as a list of "
                "dictionaries with product_id and quantity. When emailing clients, look up "
                "their email address in Odoo first if not provided. Execute tasks accurately "
                "and report concise results. When actions are declined by the operator, "
                "acknowledge the decision politely."
            ),
        }
    ]
    welcome_text = (
        "👋 *Odoo & Gmail MCP Agent Connected!*\n\n"
        "Try these actions:\n"
        "• `/check_stock` - Scan for low-inventory items\n"
        "• Create a quotation for Delta Engineering with 2 Desk Chairs and 1 Office Desk\n"
        "• Generate an invoice for order 1\n"
        "• Send me the PDF invoice for invoice 1\n"
        "• Check my latest unread emails\n"
        "• Look up Azure Interior's email in Odoo and send them a follow-up\n\n"
        "Sensitive write & email operations require inline confirmation."
    )
    await send_safe_message(update.effective_chat, welcome_text)


async def handle_callback_query(update: Update, _context: ContextTypes.DEFAULT_TYPE):
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


async def request_approval_in_telegram(
    chat_id: int, func_name: str, func_args: dict, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Send an inline approval prompt to Telegram and wait for user response."""
    approval_id = str(uuid.uuid4())[:8]
    event = asyncio.Event()
    STATE.pending_approvals[approval_id] = {"event": event, "approved": False}

    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{approval_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{approval_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    args_formatted = json.dumps(func_args, indent=2)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "⚠️ Approval Required for Action\n\n"
            f"Tool: {func_name}\nPayload:\n{args_formatted}"
        ),
        reply_markup=reply_markup,
    )

    try:
        await asyncio.wait_for(event.wait(), timeout=600.0)
        return STATE.pending_approvals[approval_id]["approved"]
    except TimeoutError:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏱️ Timed out waiting for approval on '{func_name}'. Action cancelled.",
        )
        return False
    finally:
        STATE.pending_approvals.pop(approval_id, None)


def _get_or_init_history(chat_id: int) -> list:
    """Return existing conversation history or initialize a new system prompt."""
    if chat_id not in STATE.chat_histories:
        STATE.chat_histories[chat_id] = [
            {
                "role": "system",
                "content": (
                    "You are an autonomous ERP & Communications Operations Assistant connected "
                    "to Odoo 18 and Gmail. When users want to create orders with multiple items, "
                    "find the product IDs first and pass them inside an `items` array to "
                    "`create_quotation`. When emailing clients, resolve their email from Odoo "
                    "first."
                ),
            }
        ]
    return STATE.chat_histories[chat_id]


async def _deliver_pdf_report(chat, result_text: str) -> str:
    """Decode base64 PDF tool output and send it as a Telegram document."""
    try:
        parts = result_text.split(":", 2)
        filename = f"{parts[1]}.pdf"
        pdf_bytes = base64.b64decode(parts[2])

        await chat.send_document(
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption=f"📄 Generated Report: {filename}",
        )
        return (
            f"Report '{filename}' was generated, saved on disk at '/tmp/{filename}', "
            f"and delivered to Telegram. Use attachment_path='/tmp/{filename}' if emailing it."
        )
    except (TelegramError, ValueError, IndexError, OSError) as file_exc:
        return f"PDF generated, but delivery failed: {file_exc!s}"

async def _process_tool_calls(
    chat_id: int,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tool_calls: list,
    history: list,
) -> None:
    """Execute requested LLM tool calls with HITL authorization, routing, and PDF handling."""
    for tool_call in tool_calls:
        func_name = tool_call.function.name
        func_args = json.loads(tool_call.function.arguments)

        if func_name in SENSITIVE_TOOLS:
            authorized = await request_approval_in_telegram(
                chat_id, func_name, func_args, context
            )
            if not authorized:
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": (
                            "Action rejected: The human operator declined "
                            "this operation."
                        ),
                    }
                )
                continue

        target_session = STATE.tool_sessions.get(func_name)
        if not target_session:
            result_text = f"ERROR: No MCP server registered for tool '{func_name}'."
        else:
            result_text = await execute_mcp_tool(target_session, func_name, func_args)

        if result_text.startswith("SUCCESS_PDF:"):
            result_text = await _deliver_pdf_report(update.effective_chat, result_text)

        history.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_text,
            }
        )


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route incoming Telegram user prompts through the Groq + MCP reasoning loop."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not is_authorized(user.id):
        print(f"[!] Blocked message from unauthorized user ID: {user.id}")
        await update.message.reply_text(
            f"⛔ Unauthorized access. ID `{user.id}` is not permitted.",
            parse_mode="Markdown",
        )
        return

    history = _get_or_init_history(chat_id)
    history.append({"role": "user", "content": update.message.text})

    if len(history) > 10:
        STATE.chat_histories[chat_id] = [history[0]] + history[-8:]
        history = STATE.chat_histories[chat_id]

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    for _ in range(8):
        try:
            response = await client.chat.completions.create(
                model=MODEL_NAME,
                messages=history,
                tools=STATE.groq_tools,
                tool_choice="auto",
            )
        except GroqError as exc:
            error_str = str(exc)
            print(f"[!] Groq API Error: {error_str}")
            if "rate_limit_exceeded" in error_str or "413" in error_str:
                STATE.chat_histories[chat_id] = [history[0]]
                await update.message.reply_text(
                    "⚠️ Conversation context exceeded Groq rate limits. "
                    "Memory reset. Please repeat your request."
                )
            else:
                await update.message.reply_text(f"⚠️ AI Engine error: {error_str}")
            return

        msg = response.choices[0].message
        history.append(msg)

        if not msg.tool_calls:
            await send_safe_message(update.effective_chat, msg.content or "Done.")
            break

        await _process_tool_calls(chat_id, update, context, msg.tool_calls, history)


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

    all_groq_tools = []
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

        converted = mcp_tools_to_groq_tools(tools_data)
        all_groq_tools.extend(converted)
        print(f"[+] Loaded {len(converted)} tools from {server_name} MCP.")

    STATE.groq_tools = all_groq_tools
    print(f"[+] Total MCP tools loaded into Telegram bot: {len(STATE.groq_tools)}.")


async def main():
    """Connect to Odoo and Gmail MCP servers over stdio and start the Telegram bot."""
    async with AsyncExitStack() as stack:
        await _connect_mcp_servers(stack)
        app = _build_telegram_app()

        print("[+] Initializing Telegram application...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        print("[+] Telegram polling started. Listening for messages...")

        # Launch autonomous background worker
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
