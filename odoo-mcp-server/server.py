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
    standard_price: float = 0.0,
    initial_qty: float = 0.0,
) -> str:
    """Create or update a storable product in Odoo 18 and set its initial stock quantity in the internal warehouse."""
    uid = get_uid()

    # 1. Search if product already exists
    products = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "product.product",
        "search_read",
        [[["name", "=", name]]],
        {"fields": ["id", "product_tmpl_id", "name"]},
    )

    if products:
        product_id = products[0]["id"]
        tmpl_id = (
            products[0]["product_tmpl_id"][0]
            if isinstance(products[0]["product_tmpl_id"], list)
            else products[0]["product_tmpl_id"]
        )

        # Update product template to be storable (Odoo 18 standard)
        try:
            odoo_rpc(
                "object",
                "execute_kw",
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "product.template",
                "write",
                [
                    [tmpl_id],
                    {
                        "is_storable": True,
                        "type": "consu",
                        "list_price": float(list_price),
                        "standard_price": float(standard_price),
                    },
                ],
            )
        except Exception:
            # Fallback for earlier Odoo versions
            odoo_rpc(
                "object",
                "execute_kw",
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "product.template",
                "write",
                [[tmpl_id], {"type": "product"}],
            )
    else:
        # Create product template first with Odoo 18 is_storable flag
        tmpl_vals = {
            "name": name,
            "list_price": float(list_price),
            "standard_price": float(standard_price),
            "is_storable": True,
            "type": "consu",
            "sale_ok": True,
            "purchase_ok": True,
        }
        try:
            tmpl_id = odoo_rpc(
                "object",
                "execute_kw",
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "product.template",
                "create",
                [tmpl_vals],
            )
        except Exception:
            # Fallback for pre-Odoo 18 schemas
            tmpl_vals.pop("is_storable", None)
            tmpl_vals["type"] = "product"
            tmpl_id = odoo_rpc(
                "object",
                "execute_kw",
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "product.template",
                "create",
                [tmpl_vals],
            )

        # Retrieve the product.product variant generated by Odoo
        prod_variants = odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.product",
            "search_read",
            [[["product_tmpl_id", "=", tmpl_id]]],
            {"fields": ["id"]},
        )
        product_id = prod_variants[0]["id"]

    # 2. If initial quantity requested, record stock quantity via stock.quant
    if initial_qty > 0:
        # Locate the default internal warehouse stock location
        locs = odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.location",
            "search_read",
            [[["usage", "=", "internal"]]],
            {"fields": ["id", "complete_name"], "limit": 1},
        )
        if not locs:
            return (
                f"Product '{name}' configured as storable (ID: {product_id}), but no internal "
                f"warehouse location was found to set stock."
            )

        location_id = locs[0]["id"]
        loc_name = locs[0]["complete_name"]

        try:
            # Check if quant exists
            quants = odoo_rpc(
                "object",
                "execute_kw",
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "stock.quant",
                "search_read",
                [[["product_id", "=", product_id], ["location_id", "=", location_id]]],
                {"fields": ["id"]},
            )

            if quants:
                quant_id = quants[0]["id"]
                odoo_rpc(
                    "object",
                    "execute_kw",
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "stock.quant",
                    "write",
                    [[quant_id], {"inventory_quantity": float(initial_qty)}],
                )
            else:
                quant_id = odoo_rpc(
                    "object",
                    "execute_kw",
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "stock.quant",
                    "create",
                    [
                        {
                            "product_id": product_id,
                            "location_id": location_id,
                            "inventory_quantity": float(initial_qty),
                        }
                    ],
                )

            # Apply the inventory adjustment
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

            return (
                f"✅ Storable Product '{name}' (ID: {product_id}) successfully updated!\n"
                f"• Sales Price: ${list_price:.2f}\n"
                f"• Cost Price: ${standard_price:.2f}\n"
                f"• On-Hand Quantity: {initial_qty} units\n"
                f"• Warehouse Location: {loc_name}"
            )
        except Exception as exc:
            return f"Product '{name}' is storable (ID: {product_id}), but updating stock quantity failed: {exc!s}"

    return f"✅ Storable Product '{name}' ready with ID {product_id}."


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
def create_quotation(partner_id: int, items: list[dict]) -> dict:
    """Create a draft Sales Order (Quotation) with multiple line items.

    Args:
        partner_id: ID of the customer (res.partner).
        items: List of dictionaries for each product line.
               Format: [{"product_id": int, "quantity": float, "price_unit": float (optional)}]
               Example: [{"product_id": 12, "quantity": 2.0}, {"product_id": 15, "quantity": 1.0}]
    """
    uid = get_uid()

    if not items:
        return {"status": "error", "message": "At least one item line is required."}

    order_lines = []
    for item in items:
        prod_id = item.get("product_id")
        qty = float(item.get("quantity", 1.0))

        # Build Odoo One2many creation tuple (0, 0, values)
        line_vals = {
            "product_id": prod_id,
            "product_uom_qty": qty,
        }
        if "price_unit" in item and item["price_unit"] is not None:
            line_vals["price_unit"] = float(item["price_unit"])

        order_lines.append((0, 0, line_vals))

    order_values = {
        "partner_id": partner_id,
        "order_line": order_lines,
    }

    try:
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
            {"fields": ["name", "amount_total", "state", "order_line"]},
        )

        return {
            "status": "success",
            "order_id": order_id,
            "order_name": order_data[0]["name"],
            "amount_total": order_data[0]["amount_total"],
            "line_count": len(order_data[0].get("order_line", [])),
            "state": order_data[0]["state"],
        }
    except Exception as exc:
        return {"status": "error", "message": f"Failed to create order: {exc!s}"}


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


# ============================================================================
# WAREHOUSE PICKINGS & DELIVERIES
# ============================================================================


@mcp.tool()
def get_order_deliveries(order_id: int) -> str:
    """Retrieve warehouse delivery transfers (stock.picking) linked to a specific sales order ID."""
    uid = get_uid()

    # 1. Look up the Sales Order name/reference (e.g., S00001)
    orders = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "sale.order",
        "search_read",
        [[["id", "=", int(order_id)]]],
        {"fields": ["name", "state"]},
    )
    if not orders:
        return f"Sales Order #{order_id} not found."

    order_name = orders[0]["name"]

    # 2. Query stock.picking directly by origin or sale_id domain
    pickings = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "stock.picking",
        "search_read",
        [[["origin", "=", order_name]]],
        {
            "fields": [
                "id",
                "name",
                "state",
                "origin",
                "location_id",
                "location_dest_id",
                "scheduled_date",
            ]
        },
    )

    if not pickings:
        return (
            f"No delivery transfers found matching origin '{order_name}'. "
            f"Note: Delivery transfers are only generated once the quotation is confirmed into a sale order."
        )

    results = []
    for p in pickings:
        src = (
            p["location_id"][1]
            if isinstance(p["location_id"], list)
            else p["location_id"]
        )
        dst = (
            p["location_dest_id"][1]
            if isinstance(p["location_dest_id"], list)
            else p["location_dest_id"]
        )
        results.append(
            f"🚚 **Transfer {p['name']}** (ID: {p['id']})\n"
            f"• Origin: {p.get('origin')}\n"
            f"• Status: {p['state']}\n"
            f"• Route: {src} ➔ {dst}\n"
            f"• Scheduled: {p.get('scheduled_date', 'N/A')}"
        )

    return "\n\n".join(results)


@mcp.tool()
def validate_delivery_order(picking_id: int) -> str:
    """Validate a warehouse delivery transfer (stock.picking) by automatically setting quantities done and confirming dispatch."""
    uid = get_uid()

    pickings = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "stock.picking",
        "search_read",
        [[["id", "=", int(picking_id)]]],
        {"fields": ["name", "state", "move_ids"]},
    )
    if not pickings:
        return f"Delivery Picking ID {picking_id} not found."

    picking = pickings[0]
    p_name = picking["name"]
    state = picking["state"]

    if state == "done":
        return (
            f"Delivery {p_name} (ID: {picking_id}) is already validated and completed."
        )
    if state == "cancel":
        return f"Delivery {p_name} (ID: {picking_id}) is cancelled and cannot be validated."

    try:
        # Populate quantity_done on move lines if not automatically done
        move_ids = picking.get("move_ids", [])
        if move_ids:
            moves = odoo_rpc(
                "object",
                "execute_kw",
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "stock.move",
                "search_read",
                [[["id", "in", move_ids]]],
                {"fields": ["id", "product_uom_qty", "quantity"]},
            )
            for m in moves:
                odoo_rpc(
                    "object",
                    "execute_kw",
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "stock.move",
                    "write",
                    [[m["id"]], {"quantity": m.get("product_uom_qty", 1.0)}],
                )

        # Trigger button_validate on the picking
        odoo_rpc(
            "object",
            "execute_kw",
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.picking",
            "button_validate",
            [[int(picking_id)]],
        )

        return (
            f"✅ Delivery order {p_name} (ID: {picking_id}) successfully validated and marked as completed!"
            f" Goods are officially dispatched."
        )
    except Exception as exc:
        return f"Failed to validate delivery order {p_name}: {exc!s}"


@mcp.tool()
def get_invoice_pdf(invoice_id: int) -> str:
    """Generate and return a base64-encoded PDF report for a given invoice ID (account.move)."""
    uid = get_uid()

    # 1. Verify invoice exists
    inv = odoo_rpc(
        "object",
        "execute_kw",
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.move",
        "search_read",
        [[["id", "=", int(invoice_id)]]],
        {"fields": ["name", "state"]},
    )
    if not inv:
        return f"Invoice ID {invoice_id} not found."

    inv_record = inv[0]
    inv_name = (inv_record.get("name") or f"INV_{invoice_id}").replace("/", "_")

    # 2. Authenticate an HTTP session via /web/session/authenticate
    session = requests.Session()
    auth_url = f"{ODOO_URL}/web/session/authenticate"
    auth_payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "db": ODOO_DB,
            "login": ODOO_USER,
            "password": ODOO_PASSWORD,
        },
        "id": 1,
    }

    try:
        auth_resp = session.post(auth_url, json=auth_payload, timeout=15)
        auth_resp.raise_for_status()

        # 3. Download the compiled PDF via Odoo's standard web report controller
        # In Odoo, account.report_invoice_with_payments is the primary invoice template
        report_url = (
            f"{ODOO_URL}/report/pdf/account.report_invoice_with_payments/{invoice_id}"
        )
        pdf_resp = session.get(report_url, timeout=30)

        # Fallback to standard invoice if with_payments is unavailable
        if pdf_resp.status_code != 200 or not pdf_resp.content.startswith(b"%PDF"):
            fallback_url = f"{ODOO_URL}/report/pdf/account.report_invoice/{invoice_id}"
            pdf_resp = session.get(fallback_url, timeout=30)

        if pdf_resp.status_code == 200 and pdf_resp.content.startswith(b"%PDF"):
            b64_pdf = base64.b64encode(pdf_resp.content).decode("utf-8")
            return f"SUCCESS_PDF:{inv_name}:{b64_pdf}"
        else:
            return (
                f"Failed to fetch PDF report from Odoo web controller. "
                f"Status: {pdf_resp.status_code}, Response snippet: {pdf_resp.text[:200]}"
            )

    except Exception as exc:
        return f"Error downloading invoice report: {exc!s}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
