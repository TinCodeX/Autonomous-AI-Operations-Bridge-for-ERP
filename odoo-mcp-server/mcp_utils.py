"""Shared MCP utility helpers for Odoo agents."""

from anyio import BrokenResourceError, ClosedResourceError
from mcp import ClientSession
from mcp.shared.exceptions import McpError

MCP_TOOL_ERRORS = (
    McpError,
    BrokenResourceError,
    ClosedResourceError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
)


def mcp_tools_to_groq_tools(tools_result) -> list:
    """Format MCP tool definitions into OpenAI/Groq function-calling schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema or {"type": "object", "properties": {}},
            },
        }
        for tool in tools_result.tools
    ]


async def execute_mcp_tool(
    session: ClientSession, func_name: str, func_args: dict
) -> str:
    """Invoke an MCP tool and return its concatenated text output or error."""
    try:
        result = await session.call_tool(func_name, arguments=func_args)
        return "".join(part.text for part in result.content if hasattr(part, "text"))
    except MCP_TOOL_ERRORS as exc:
        return f"Tool execution failed: {exc!s}"
