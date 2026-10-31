"""Odoo Model Context Protocol (MCP) Server bridge."""

import json
import os
import requests
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("odoo-mcp-server")

# Odoo Connection Configuration
ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "firstmcptinsaeodoo")
ODOO_USER = os.getenv("ODOO_USER", "tinsaebefekadu2012@gmail.com")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "P@97$s05w016")


def odoo_rpc(service: str, method: str, *args):
    """Helper to send JSON-RPC calls to Odoo."""
    endpoint = f"{ODOO_URL}/jsonrpc"
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": service,
            "method": method,
            "args": list(args),
        },
        "id": 1,
    }
    response = requests.post(
        endpoint,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"Odoo RPC Error: {data['error']}")
    return data.get("result")


def get_uid() -> int:
    """Authenticate with Odoo and return the User ID (UID)."""
    uid = odoo_rpc("common", "authenticate", ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise ConnectionRefusedError(
            "Authentication failed. Check your ODOO_USER or ODOO_PASSWORD."
        )
    return uid


@mcp.tool()
def search_customers(name: str = "") -> str:
    """Search for contacts/customers in Odoo by name."""
    uid = get_uid()
    domain = [["name", "ilike", name]] if name else []
    fields = ["id", "name", "email", "phone", "city"]

    records = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "search_read",
        [domain],
        {"fields": fields, "limit": 5},
    )
    return json.dumps(records, indent=2)

@mcp.tool()
def create_quotation(
    partner_id: int, product_id: int, quantity: float = 1.0
) -> dict:
    """Create a draft Sales Order (Quotation) with an itemized product line."""
    uid = get_uid()

    # Odoo ORM convention for one2many relation: (0, 0, {values})
    order_values = {
        "partner_id": partner_id,
        "order_line": [
            (
                0,
                0,
                {
                    "product_id": product_id,
                    "product_uom_qty": quantity,
                },
            )
        ],
    }

    order_id = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "create",
        [order_values],
    )

    # Fetch quotation reference code (e.g. S00004) and total amount
    order_data = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "read",
        [[order_id]],
        {"fields": ["name", "amount_total", "state"]},
    )

    return {
        "status": "success",
        "order_id": order_id,
        "order_name": order_data[0]["name"],
        "amount_total": order_data[0]["amount_total"],
        "state": order_data[0]["state"],
    }

@mcp.tool()
def get_recent_quotations(limit: int = 5) -> str:
    """Retrieve recent sales quotations and orders from Odoo."""
    uid = get_uid()
    fields = ["name", "partner_id", "date_order", "amount_total", "state"]

    orders = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "search_read",
        [[]],
        {"fields": fields, "limit": limit},
    )
    return json.dumps(orders, indent=2)

@mcp.tool()
def search_products(query: str = "") -> list:
    """Search for sellable products by name or internal reference."""
    uid = get_uid()
    domain = [("sale_ok", "=", True)]
    if query:
        domain.append(("name", "ilike", query))
    fields = ["id", "name", "list_price", "default_code", "type"]
    return odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [domain],
        {"fields": fields, "limit": 10},
    )

@mcp.tool()
def create_customer(
    name: str, email: str = "", phone: str = "", city: str = ""
) -> str:
    """Create a new customer/contact record in Odoo."""
    uid = get_uid()
    values = {
        "name": name,
        "email": email,
        "phone": phone,
        "city": city,
        "is_company": True,
    }

    partner_id = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.partner",
        "create",
        [values],
    )
    return json.dumps({"status": "success", "partner_id": partner_id})


if __name__ == "__main__":
    mcp.run(transport="stdio")