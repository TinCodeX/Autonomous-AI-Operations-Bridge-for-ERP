"""Odoo Model Context Protocol (MCP) Server bridge."""

import json
import os

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("odoo-mcp-server")

# Load variables from .env file
load_dotenv()

# Read credentials dynamically from the environment
ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "firstmcptinsaeodoo")
ODOO_USER = os.getenv("ODOO_USER", "admin")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "admin")


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
def create_customer(name: str, email: str = "", phone: str = "", city: str = "") -> str:
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
def add_product_to_stock(
    name: str,
    list_price: float,
    initial_qty: float = 10.0,
    standard_price: float = 0.0,
) -> str:
    """Create a new storable product in Odoo and set its initial on-hand stock quantity.

    Args:
        name: Name of the product (e.g., 'Ergonomic Desk Chair').
        list_price: Sales price for customers.
        initial_qty: Starting quantity to add to physical stock (default 10.0).
        standard_price: Cost price to buy or produce the product.
    """
    uid = get_uid()

    try:
        # 1. Create the product item
        product_vals = {
            "name": name,
            "list_price": float(list_price),
            "standard_price": float(standard_price),
            "type": "consu",  # In Odoo 18, tracked inventory uses 'consu'
        }
        product_id = odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.product",
            "create",
            [product_vals],
        )

        # 2. Look up the main internal warehouse location
        location_ids = odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.location",
            "search",
            [[["usage", "=", "internal"]]],
            {"limit": 1},
        )
        location_id = location_ids[0] if location_ids else 8

        # 3. Create stock quant and apply inventory count
        quant_vals = {
            "product_id": product_id,
            "location_id": location_id,
            "inventory_quantity": float(initial_qty),
        }

        quant_id = odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.quant",
            "create",
            [quant_vals],
        )
        odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.quant",
            "action_apply_inventory",
            [[quant_id]],
        )

        return json.dumps(
            {
                "status": "success",
                "product_id": product_id,
                "product_name": name,
                "sales_price": list_price,
                "quantity_on_hand": initial_qty,
                "message": f"Successfully created product '{name}' with {initial_qty} units in stock.",
            }
        )

    except Exception as e:
        return json.dumps(
            {
                "status": "error",
                "message": f"Failed to add product to stock: {e!s}",
            }
        )


@mcp.tool()
def create_quotation(partner_id: int, product_id: int, quantity: float = 1.0) -> dict:
    """Create a draft Sales Order (Quotation) with an itemized product line."""
    uid = get_uid()

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
def create_invoice_for_order(order_id: int) -> str:
    """Generate a draft customer invoice from a confirmed sales order ID."""
    uid = authenticate_odoo()
    if not uid:
        return "Authentication to Odoo failed."

    # 1. Verify the order exists and is in 'sale' state
    orders = execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "search_read",
        [[["id", "=", order_id]]],
        {
            "fields": [
                "name",
                "state",
                "partner_id",
                "amount_total",
                "invoice_ids",
            ]
        },
    )

    if not orders:
        return f"Sales Order ID {order_id} not found."

    order = orders[0]
    order_name = order["name"]
    state = order["state"]

    if state not in ("sale", "done"):
        return f"Cannot invoice Order {order_name} (ID: {order_id}) because its state is '{state}'. Please confirm the sales order first."

    # 2. Trigger Odoo's invoice creation method
    try:
        invoice_ids = execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "_create_invoices",
            [[order_id]],
        )
    except Exception as exc:
        return f"Failed to generate invoice for {order_name}: {exc!s}"

    if not invoice_ids:
        # Check if already fully invoiced
        refreshed = execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "search_read",
            [[["id", "=", order_id]]],
            {"fields": ["invoice_ids"]},
        )
        existing_ids = refreshed[0].get("invoice_ids", [])
        if existing_ids:
            return f"Order {order_name} already has invoice(s) generated: ID(s) {existing_ids}."
        return f"No invoiceable lines found for {order_name} (check product invoicing policy, e.g., ordered vs. delivered quantities)."

    # 3. Read generated invoice details
    invoices = execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.move",
        "search_read",
        [[["id", "in", invoice_ids]]],
        {
            "fields": [
                "name",
                "state",
                "amount_total",
                "partner_id",
                "invoice_date",
            ]
        },
    )

    inv = invoices[0]
    inv_name = inv.get("name") or "Draft Invoice"
    amount = inv.get("amount_total", 0.0)
    customer = (
        inv["partner_id"][1] if isinstance(inv.get("partner_id"), list) else "Unknown"
    )

    return (
        f"✅ Invoice created successfully!\n"
        f"• Invoice ID: {inv['id']}\n"
        f"• Number / Reference: {inv_name}\n"
        f"• Customer: {customer}\n"
        f"• Total Amount: {amount}\n"
        f"• Origin Order: {order_name}"
    )


@mcp.tool()
def confirm_sale_order(order_id: int) -> str:
    """Confirm a draft sales quotation into a confirmed sales order."""
    uid = authenticate_odoo()
    execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "action_confirm", [[order_id]]
    )
    return f"Sales Order #{order_id} confirmed successfully."


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
