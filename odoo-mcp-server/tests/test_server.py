"""Smoke test for Odoo MCP Server tool definitions."""

from server import mcp


def test_registered_tools():
    """Verify that all 5 essential ERP tools are registered with FastMCP."""
    tool_names = [tool.name for tool in mcp._tool_manager.list_tools()]
    expected_tools = [
        "search_customers",
        "create_customer",
        "search_products",
        "create_quotation",
        "get_recent_quotations",
    ]
    for expected in expected_tools:
        assert expected in tool_names, f"Tool '{expected}' is missing from MCP server"
