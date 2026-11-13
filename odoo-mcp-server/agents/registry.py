"""Domain-scoped tool partitioning for specialized worker agents."""

from typing import Any

AGENT_TOOL_ALLOWLIST: dict[str, set[str]] = {
    "sales": {
        "search_customers",
        "create_customer",
        "list_products",
        "create_quotation",
        "confirm_order",
        "list_invoices",
        "generate_pdf_report",
    },
    "inventory": {
        "list_products",
        "check_low_stock",
        "update_product_stock",
    },
    "comms": {
        "search_emails",
        "read_email",
        "send_email",
    },
}


def get_tools_for_agent(
    agent_name: str, all_groq_tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return only the Groq-formatted tools permitted for the given specialist."""
    allowed = AGENT_TOOL_ALLOWLIST.get(agent_name, set())
    return [
        tool
        for tool in all_groq_tools
        if tool.get("function", {}).get("name") in allowed
    ]