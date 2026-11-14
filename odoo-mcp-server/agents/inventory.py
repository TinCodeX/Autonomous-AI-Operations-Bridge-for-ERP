"""Inventory & Fulfillment specialist node for stock levels and delivery orders."""

from typing import Any

from agents.state import AgentState
from agents.worker_base import run_react_worker

INVENTORY_ALLOWED_TOOLS: set[str] = {
    "search_products",
    "get_stock_level",
    "get_low_stock_products",
    "add_product_to_stock",
    "get_order_deliveries",
    "validate_delivery_order",
}

INVENTORY_SYSTEM_PROMPT: str = (
    "You are the Odoo Inventory & Fulfillment Specialist. Handle stock checks, "
    "low-stock audits, stock additions, and delivery order validations. "
    "Never invent product or picking IDs—look them up first."
)


async def inventory_node(state: AgentState, deps: dict[str, Any]) -> dict[str, Any]:
    """Execute the Inventory & Fulfillment specialist node."""
    return await run_react_worker(
        agent_name="inventory",
        system_prompt=INVENTORY_SYSTEM_PROMPT,
        allowed_tools=INVENTORY_ALLOWED_TOOLS,
        state=state,
        deps=deps,
    )
