"""Odoo Model Context Protocol (MCP) Server bridge."""

import base64
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
ODOO_URL = os.getenv("ODOO_URL", "http://127.0.0.1:8069")
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
        timeout=15,
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


# ============================================================================
# CONTACTS & CUSTOMERS
# ============================================================================


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


# ============================================================================
# PRODUCTS & INVENTORY
# ============================================================================


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
def get_stock_level(product_name_or_id: str) -> str:
    """Get the physical on-hand quantity and reserved stock for a product by name or ID."""
    uid = get_uid()

    domain = (
        [["id", "=", int(product_name_or_id)]]
        if product_name_or_id.isdigit()
        else [["name", "ilike", product_name_or_id]]
    )

    products = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [domain],
        {"fields": ["id", "name", "qty_available", "virtual_available"], "limit": 5},
    )

    if not products:
        return f"No product found matching '{product_name_or_id}'."

    results = []
    for prod in products:
        p_id = prod["id"]
        p_name = prod["name"]
        on_hand = prod.get("qty_available", 0.0)
        forecasted = prod.get("virtual_available", 0.0)

        quants = odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.quant",
            "search_read",
            [[["product_id", "=", p_id], ["location_id.usage", "=", "internal"]]],
            {"fields": ["location_id", "quantity", "reserved_quantity"]},
        )

        locations_detail = []
        for q in quants:
            loc_name = (
                q["location_id"][1]
                if isinstance(q.get("location_id"), list)
                else str(q.get("location_id"))
            )
            locations_detail.append(
                f"- {loc_name}: {q.get('quantity', 0.0)} units (Reserved: {q.get('reserved_quantity', 0.0)})"
            )

        detail_str = (
            "\n  ".join(locations_detail)
            if locations_detail
            else "No internal quant records found."
        )
        results.append(
            f"📦 **{p_name}** (ID: {p_id})\n"
            f"• Total On-Hand: {on_hand}\n"
            f"• Forecasted Available: {forecasted}\n"
            f"• Location Breakdown:\n  {detail_str}"
        )

    return "\n\n".join(results)


@mcp.tool()
def add_product_to_stock(
    name: str,
    list_price: float,
    initial_qty: float = 10.0,
    standard_price: float = 0.0,
) -> str:
    """Create a new storable product in Odoo and set its initial on-hand stock quantity."""
    uid = get_uid()

    try:
        product_vals = {
            "name": name,
            "list_price": float(list_price),
            "standard_price": float(standard_price),
            "type": "consu",  # Tracked inventory in Odoo 18
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


# ============================================================================
# SALES & QUOTATIONS
# ============================================================================


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
def confirm_sale_order(order_id: int) -> str:
    """Confirm a draft sales quotation into a confirmed sales order."""
    uid = get_uid()
    odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "action_confirm",
        [[order_id]],
    )
    return f"Sales Order #{order_id} confirmed successfully."


# ============================================================================
# INVOICING & REPORTS
# ============================================================================


@mcp.tool()
def create_invoice_for_order(order_id: int) -> str:
    """Generate a draft customer invoice from a confirmed sales order ID."""
    uid = get_uid()

    # 1. Verify the order exists and check its status & partner
    orders = odoo_rpc(
        "object",
        "execute_kw",
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

    # 2. Invoke the standard Odoo invoicing wizard: sale.advance.payment.inv
    try:
        # Context tells the wizard which sale orders are being targeted
        wizard_context = {
            "active_model": "sale.order",
            "active_ids": [order_id],
            "active_id": order_id,
        }

        # Create the wizard instance ('deliver' creates regular invoice for deliverable/ordered lines)
        wizard_vals = {
            "advance_payment_method": "delivered",
        }

        wizard_id = odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.advance.payment.inv",
            "create",
            [wizard_vals],
            {"context": wizard_context},
        )

        # Call public action method on the wizard
        odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.advance.payment.inv",
            "create_invoices",
            [[wizard_id]],
            {"context": wizard_context},
        )
    except Exception as exc:
        return f"Failed to generate invoice via wizard for {order_name}: {exc!s}"

    # 3. Read back the updated order to fetch newly linked invoice IDs
    refreshed_order = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "search_read",
        [[["id", "=", order_id]]],
        {"fields": ["invoice_ids"]},
    )

    invoice_ids = refreshed_order[0].get("invoice_ids", []) if refreshed_order else []
    if not invoice_ids:
        return (
            f"Wizard ran, but no invoice was created for {order_name}. "
            "Ensure the products on this order have quantities ready for invoicing "
            "(check Invoicing Policy: Ordered vs Delivered quantities)."
        )

    newest_invoice_id = invoice_ids[-1]

    # 4. Read details from account.move
    invoices = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.move",
        "search_read",
        [[["id", "=", newest_invoice_id]]],
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
def get_invoice_pdf(invoice_id: int) -> str:
    """Generate and return a base64-encoded PDF report for a given invoice ID (account.move)."""
    uid = get_uid()

    inv = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.move",
        "search_read",
        [[["id", "=", invoice_id]]],
        {"fields": ["name", "state"]},
    )
    if not inv:
        return f"Invoice ID {invoice_id} not found."

    inv_record = inv[0]
    inv_name = (inv_record.get("name") or f"INV_{invoice_id}").replace("/", "_")

    try:
        pdf_content, _ = odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "ir.actions.report",
            "_render_qweb_pdf",
            ["account.report_invoice", [invoice_id]],
        )

        if isinstance(pdf_content, bytes):
            b64_pdf = base64.b64encode(pdf_content).decode("utf-8")
        else:
            b64_pdf = pdf_content

        return f"SUCCESS_PDF:{inv_name}:{b64_pdf}"
    except Exception as exc:
        return f"Error rendering invoice PDF: {exc!s}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
