"""Interactive Autonomous MCP Agent Runner for Odoo with Human-in-the-Loop Confirmation."""

import asyncio
import json
import os
import sys
from dotenv import load_dotenv
from groq import Groq
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

# 1. LLM Client Setup
client = Groq(api_key=os.getenv("GROQ_API_KEY"))
MODEL_NAME = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

# Define tools that require explicit human approval before running
SENSITIVE_TOOLS = {"create_customer", "create_quotation"}


def mcp_tools_to_groq_tools(mcp_tools) -> list:
    """Format MCP tool definitions into OpenAI/Groq function-calling schema."""
    groq_tools = []
    for tool in mcp_tools.tools:
        groq_tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema
                or {"type": "object", "properties": {}},
            },
        })
    return groq_tools


def ask_human_approval(func_name: str, func_args: dict) -> bool:
    """Prompt the operator to confirm an action before writing to Odoo."""
    print("\n" + "=" * 55)
    print(f"⚠️  HUMAN APPROVAL REQUIRED FOR ACTION: [{func_name}]")
    print(f"    Payload: {json.dumps(func_args, indent=2)}")
    print("=" * 55)

    while True:
        try:
            choice = input("Authorize this execution? [y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if choice in ("y", "yes"):
            return True
        elif choice in ("n", "no"):
            return False
        print("Please enter 'y' to confirm or 'n' to reject.")


async def process_user_turn(session: ClientSession, groq_tools: list, messages: list):
    """Execute the multi-step reasoning and tool-calling loop for one turn."""
    max_iterations = 8
    iteration = 0

    while iteration < max_iterations:
        iteration += 1

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=groq_tools,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        messages.append(msg)

        # If LLM produces no tool calls, it has reached its conclusion
        if not msg.tool_calls:
            print(f"\n[Agent]: {msg.content}\n")
            break

        # Process tool invocations
        for tool_call in msg.tool_calls:
            func_name = tool_call.function.name
            func_args = json.loads(tool_call.function.arguments)

            # --- Human-in-the-Loop Interception ---
            if func_name in SENSITIVE_TOOLS:
                authorized = ask_human_approval(func_name, func_args)
                if not authorized:
                    print(f"[-] Action '{func_name}' rejected by human operator.")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": "Action rejected: The human operator declined permission to perform this change.",
                    })
                    continue

            print(f"[*] Calling Tool: {func_name} -> {func_args}")

            try:
                result = await session.call_tool(func_name, arguments=func_args)
                result_text = "".join(
                    [part.text for part in result.content if hasattr(part, "text")]
                )
            except Exception as exc:
                result_text = f"Tool execution failed: {str(exc)}"

            print(f"[<] Tool Response: {result_text[:120]}...")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_text,
            })


async def run_interactive_session():
    """Start an interactive CLI loop connected to the Odoo MCP Server."""
    python_executable = os.path.join(os.getcwd(), "venv", "Scripts", "python.exe")
    server_script = os.path.join(os.getcwd(), "server.py")

    server_params = StdioServerParameters(
        command=python_executable,
        args=[server_script],
        env=None,
    )

    print("\n[+] Starting Odoo MCP server over stdio...")
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Dynamic tool discovery
            tool_list = await session.list_tools()
            groq_tools = mcp_tools_to_groq_tools(tool_list)
            loaded_names = [t["function"]["name"] for t in groq_tools]
            print(f"[+] Server ready. Loaded tools ({len(loaded_names)}): {loaded_names}")
            print(f"[+] Human-in-the-Loop active for: {list(SENSITIVE_TOOLS)}")
            print("[+] Type 'exit' or 'quit' to end the session.\n")

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are an autonomous ERP Operations Assistant connected "
                        "to a live Odoo 18 instance. You have access to tools for "
                        "searching and creating customers, searching products, "
                        "creating quotations, and viewing orders. Step by step, execute "
                        "the requested operations and report clean, concrete results. "
                        "If a human operator rejects an action, adapt gracefully and inform them."
                    ),
                }
            ]

            while True:
                try:
                    user_input = input("[You]: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nExiting session...")
                    break

                if not user_input:
                    continue

                if user_input.lower() in ("exit", "quit"):
                    print("Exiting session...")
                    break

                messages.append({"role": "user", "content": user_input})
                await process_user_turn(session, groq_tools, messages)


if __name__ == "__main__":
    asyncio.run(run_interactive_session())