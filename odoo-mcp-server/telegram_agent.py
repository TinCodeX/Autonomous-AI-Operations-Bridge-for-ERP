"""Telegram Bot Interface for Odoo MCP Agent with Inline Human-in-the-Loop."""

import asyncio
import json
import os
import uuid

from dotenv import load_dotenv
from groq import AsyncGroq
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
MODEL_NAME = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
SENSITIVE_TOOLS = {
    "create_customer",
    "create_quotation",
    "add_product_to_stock",
    "confirm_sale_order",
    "create_invoice_for_order",
}

mcp_session: ClientSession | None = None
groq_tools: list = []
# Structure: {approval_id: {"event": asyncio.Event(), "approved": bool}}
pending_approvals: dict[str, dict] = {}
chat_histories: dict[int, list] = {}


def mcp_tools_to_groq_tools(tools_result) -> list:
    """Format FastMCP tool definitions into Groq function-calling schema."""
    converted = []
    for tool in tools_result.tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return converted


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_histories[chat_id] = [
        {
            "role": "system",
            "content": (
                "You are an autonomous ERP Operations Assistant connected to Odoo 18. "
                "You have access to tools for customer, quotation, and inventory management. "
                "Execute tasks accurately and report concise results. When actions are "
                "declined by the operator, acknowledge the decision politely."
            ),
        }
    ]
    await update.message.reply_text(
        "👋 *Odoo ERP Agent Connected!*\n\n"
        "Send requests in natural language, such as:\n"
        "• _Show 3 products in inventory_\n"
        "• _Check if Delta Engineering exists_\n"
        "• _Create a quotation for Delta Engineering_\n"
        "• _Add a new product to stock_\n\n"
        "Sensitive write operations will ask for confirmation here.",
        parse_mode="Markdown",
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button clicks for Approve/Reject resiliently."""
    query = update.callback_query
    if not query:
        return

    print(f"\n[DEBUG] Click received: callback_data = {query.data}")

    try:
        await query.answer()
    except Exception as exc:
        print(f"[!] Warning on query.answer: {exc}")

    data = query.data or ""
    if not (data.startswith("approve:") or data.startswith("reject:")):
        return

    action, approval_id = data.split(":", 1)
    record = pending_approvals.get(approval_id)

    if not record:
        print(f"[!] Stale button tapped: ID {approval_id} not found in pending map.")
        try:
            await query.edit_message_text(
                f"{query.message.text}\n\n⚠️ [SESSION EXPIRED / TIMED OUT]"
            )
        except Exception:
            pass
        return

    is_approved = action == "approve"
    record["approved"] = is_approved
    record["event"].set()
    print(f"[DEBUG] Event set for ID {approval_id}: approved = {is_approved}")

    status_tag = (
        "✅ [AUTHORIZED BY OPERATOR]" if is_approved else "❌ [REJECTED BY OPERATOR]"
    )
    try:
        current_text = query.message.text or ""
        await query.edit_message_text(f"{current_text}\n\n{status_tag}")
    except Exception as exc:
        print(f"[!] Warning on edit_message_text: {exc}")


async def request_approval_in_telegram(
    chat_id: int, func_name: str, func_args: dict, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    approval_id = str(uuid.uuid4())[:8]
    event = asyncio.Event()
    pending_approvals[approval_id] = {"event": event, "approved": False}

    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{approval_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{approval_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    args_formatted = json.dumps(func_args, indent=2)
    message_text = (
        f"⚠️ Approval Required for ERP Action\n\n"
        f"Tool: {func_name}\n"
        f"Payload:\n{args_formatted}"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=message_text,
        reply_markup=reply_markup,
    )

    try:
        # 10 minute wait window
        await asyncio.wait_for(event.wait(), timeout=600.0)
        return pending_approvals[approval_id]["approved"]
    except TimeoutError:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏱️ Timed out waiting for approval on '{func_name}'. Action cancelled.",
        )
        return False
    finally:
        pending_approvals.pop(approval_id, None)


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_prompt = update.message.text

    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system",
                "content": "You are an autonomous ERP Operations Assistant connected to Odoo 18.",
            }
        ]

    history = chat_histories[chat_id]
    history.append({"role": "user", "content": user_prompt})

    # Sliding window: keep system prompt + last 8 conversational turns
    if len(history) > 10:
        chat_histories[chat_id] = [history[0]] + history[-8:]
        history = chat_histories[chat_id]

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    iteration = 0
    max_iterations = 8

    while iteration < max_iterations:
        iteration += 1

        try:
            response = await client.chat.completions.create(
                model=MODEL_NAME,
                messages=history,
                tools=groq_tools,
                tool_choice="auto",
            )
        except Exception as exc:
            error_str = str(exc)
            print(f"[!] Groq API Error: {error_str}")
            if "rate_limit_exceeded" in error_str or "413" in error_str:
                # Reset chat history to recover immediately from token overflows
                chat_histories[chat_id] = [history[0]]
                await update.message.reply_text(
                    "⚠️ Conversation context exceeded Groq rate limits. Memory has been reset. Please repeat your query."
                )
            else:
                await update.message.reply_text(f"⚠️ Error from AI engine: {error_str}")
            return

        msg = response.choices[0].message
        history.append(msg)

        if not msg.tool_calls:
            try:
                await update.message.reply_text(
                    msg.content or "Done.", parse_mode="Markdown"
                )
            except Exception:
                await update.message.reply_text(msg.content or "Done.")
            break

        for tool_call in msg.tool_calls:
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
                            "content": "Action rejected: The human operator declined this operation.",
                        }
                    )
                    continue

            try:
                result = await mcp_session.call_tool(func_name, arguments=func_args)
                result_text = "".join(
                    [part.text for part in result.content if hasattr(part, "text")]
                )
            except Exception as exc:
                result_text = f"Tool execution failed: {exc!s}"

            history.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_text,
                }
            )


async def main():
    global mcp_session, groq_tools

    python_executable = os.path.join(os.getcwd(), "venv", "Scripts", "python.exe")
    server_script = os.path.join(os.getcwd(), "server.py")

    server_params = StdioServerParameters(
        command=python_executable,
        args=[server_script],
        env=None,
    )

    print("[+] Starting Odoo MCP server over stdio...")
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            mcp_session = session

            tools_data = await session.list_tools()
            groq_tools = mcp_tools_to_groq_tools(tools_data)
            print(
                f"[+] MCP connected. Loaded {len(groq_tools)} tools into Telegram bot."
            )

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
            app.add_handler(CallbackQueryHandler(handle_callback_query))
            app.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message)
            )

            print("[+] Initializing Telegram application...")
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            print("[+] Telegram polling started. Listening for messages...")

            stop_event = asyncio.Event()
            try:
                await stop_event.wait()
            finally:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[!] Telegram bot terminated.")
