"""Sales & Billing specialist node for customer, quotation, and invoice operations."""

from typing import Any

from agents.state import AgentState
from agents.worker_base import run_react_worker

SALES_ALLOWED_TOOLS: set[str] = {
    "search_customers",
    "create_customer",
    "search_products",
    "get_recent_quotations",
    "create_quotation",
    "confirm_sale_order",
    "create_invoice_for_order",
    "get_invoice_pdf",
}

SALES_SYSTEM_PROMPT: str = (
    "You are the Odoo Sales & Billing Specialist. Handle customer records, "
    "quotations, sale order confirmations, invoices, and PDF generation. "
    "Never invent IDs—look them up first. Return concise, factual results."
)


async def sales_node(state: AgentState, deps: dict[str, Any]) -> dict[str, Any]:
    """Execute the Sales & Billing specialist node."""
    return await run_react_worker(
        agent_name="sales",
        system_prompt=SALES_SYSTEM_PROMPT,
        allowed_tools=SALES_ALLOWED_TOOLS,
        state=state,
        deps=deps,
    )
