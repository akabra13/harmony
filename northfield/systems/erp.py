"""The ERP connector.

Stands in for what would be an OData client against SAP or an equivalent. It is
plain data access: no authorisation, no auditing, no idempotency. All three live
one layer up, in the tool invoker and the context broker, which is what keeps them
consistent across every system rather than re-implemented per connector.

Reachable only from ``northfield/providers/`` and ``northfield/tools/``.
``tests/architecture/test_write_chokepoint.py`` enforces that.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from harmony.kernel.clock import parse_date
from harmony.kernel.store import Store, dump_json, load_json


# --- parts ---------------------------------------------------------------------


def get_part(store: Store, part_id: str) -> dict[str, Any] | None:
    row = store.query_one("SELECT * FROM parts WHERE part_id = ?", (part_id,))
    return _part(row) if row else None


def list_parts(store: Store) -> list[dict[str, Any]]:
    return [_part(r) for r in store.query("SELECT * FROM parts ORDER BY part_id")]


def _part(row) -> dict[str, Any]:
    return {
        "part_id": row["part_id"],
        "description": row["description"],
        "on_hand": row["on_hand"],
        "daily_usage": row["daily_usage"],
        "safety_stock": row["safety_stock"],
        "unit_cost": row["unit_cost"],
        "lot_tracked": bool(row["lot_tracked"]),
    }


# --- suppliers -----------------------------------------------------------------


def get_supplier(store: Store, supplier_id: str) -> dict[str, Any] | None:
    row = store.query_one("SELECT * FROM suppliers WHERE supplier_id = ?", (supplier_id,))
    return _supplier(row) if row else None


def list_suppliers(store: Store) -> list[dict[str, Any]]:
    return [_supplier(r) for r in store.query("SELECT * FROM suppliers ORDER BY supplier_id")]


def suppliers_approved_for(store: Store, part_id: str) -> list[dict[str, Any]]:
    """Suppliers qualified to supply a specific part.

    Both conditions matter: the vendor must be approved *and* the part must be on
    their qualification list. Apex Rapid Supply satisfies the first and not the
    second, which is the whole point of that record.
    """
    return [
        s
        for s in list_suppliers(store)
        if s["approved"] and part_id in s["approved_parts"]
    ]


def _supplier(row) -> dict[str, Any]:
    return {
        "supplier_id": row["supplier_id"],
        "name": row["name"],
        "contact_email": row["contact_email"],
        "approved": bool(row["approved"]),
        "approved_parts": load_json(row["approved_parts"], []),
        "lead_time_days": row["lead_time_days"],
        "pricing": load_json(row["pricing"], {}),
        "on_time_rate": row["on_time_rate"],
    }


# --- purchase orders -----------------------------------------------------------


def get_purchase_order(store: Store, po_id: str) -> dict[str, Any] | None:
    row = store.query_one("SELECT * FROM purchase_orders WHERE po_id = ?", (po_id,))
    return _po(row) if row else None


def list_purchase_orders(
    store: Store, *, part_id: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if part_id:
        clauses.append("part_id = ?")
        params.append(part_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return [
        _po(r)
        for r in store.query(f"SELECT * FROM purchase_orders {where} ORDER BY po_id", params)
    ]


def open_purchase_orders(store: Store) -> list[dict[str, Any]]:
    return list_purchase_orders(store, status="open")


def create_purchase_order(
    store: Store,
    *,
    po_id: str,
    part_id: str,
    supplier_id: str,
    qty: int,
    unit_price: float,
    ordered_date: _dt.date,
    promised_date: _dt.date,
    created_by: str,
    notes: str = "",
    replaces_po: str | None = None,
) -> dict[str, Any]:
    store.execute(
        """
        INSERT INTO purchase_orders (
            po_id, part_id, supplier_id, qty, unit_price, total_value,
            ordered_date, promised_date, status, created_by, notes, replaces_po
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (
            po_id,
            part_id,
            supplier_id,
            qty,
            unit_price,
            round(qty * unit_price, 2),
            ordered_date.isoformat(),
            promised_date.isoformat(),
            created_by,
            notes,
            replaces_po,
        ),
    )
    return get_purchase_order(store, po_id)  # type: ignore[return-value]


def set_purchase_order_status(
    store: Store, po_id: str, status: str, *, note: str | None = None
) -> dict[str, Any] | None:
    store.execute(
        "UPDATE purchase_orders SET status = ?, notes = COALESCE(?, notes) WHERE po_id = ?",
        (status, note, po_id),
    )
    return get_purchase_order(store, po_id)


def set_purchase_order_qty(store: Store, po_id: str, qty: int) -> dict[str, Any] | None:
    row = get_purchase_order(store, po_id)
    if row is None:
        return None
    store.execute(
        "UPDATE purchase_orders SET qty = ?, total_value = ? WHERE po_id = ?",
        (qty, round(qty * row["unit_price"], 2), po_id),
    )
    return get_purchase_order(store, po_id)


def _po(row) -> dict[str, Any]:
    return {
        "po_id": row["po_id"],
        "part_id": row["part_id"],
        "supplier_id": row["supplier_id"],
        "qty": row["qty"],
        "unit_price": row["unit_price"],
        "total_value": row["total_value"],
        "ordered_date": row["ordered_date"],
        "promised_date": row["promised_date"],
        "status": row["status"],
        "created_by": row["created_by"],
        "notes": row["notes"],
        "replaces_po": row["replaces_po"],
    }


# --- production orders ---------------------------------------------------------


def get_production_order(store: Store, prod_order_id: str) -> dict[str, Any] | None:
    row = store.query_one(
        "SELECT * FROM production_orders WHERE prod_order_id = ?", (prod_order_id,)
    )
    return _prod(row) if row else None


def production_orders_starting_within(
    store: Store, *, today: _dt.date, horizon_days: int
) -> list[dict[str, Any]]:
    """Planned orders whose start falls inside the horizon.

    In-progress and completed orders are excluded: their material was committed
    when they started, so a shortfall against them is a different problem with a
    different response.
    """
    until = (today + _dt.timedelta(days=horizon_days)).isoformat()
    rows = store.query(
        """
        SELECT * FROM production_orders
        WHERE status = 'planned' AND scheduled_start >= ? AND scheduled_start <= ?
        ORDER BY scheduled_start
        """,
        (today.isoformat(), until),
    )
    return [_prod(r) for r in rows]


def _prod(row) -> dict[str, Any]:
    return {
        "prod_order_id": row["prod_order_id"],
        "product": row["product"],
        "qty": row["qty"],
        "scheduled_start": row["scheduled_start"],
        "scheduled_end": row["scheduled_end"],
        "status": row["status"],
        "line": row["line"],
        "supervisor_id": row["supervisor_id"],
        "components": load_json(row["components"], []),
    }


# --- goods receipts ------------------------------------------------------------


def receipts_for_po(store: Store, po_id: str) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT * FROM goods_receipts WHERE po_id = ? ORDER BY received_date", (po_id,)
    )
    return [dict(r) for r in rows]


def record_goods_receipt(
    store: Store,
    *,
    receipt_id: str,
    po_id: str,
    part_id: str,
    qty: int,
    received_date: _dt.date,
    received_by: str,
) -> dict[str, Any]:
    store.execute(
        """
        INSERT INTO goods_receipts (receipt_id, po_id, part_id, qty, received_date, received_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (receipt_id, po_id, part_id, qty, received_date.isoformat(), received_by),
    )
    store.execute(
        "UPDATE parts SET on_hand = on_hand + ? WHERE part_id = ?", (qty, part_id)
    )
    return {"receipt_id": receipt_id, "po_id": po_id, "qty": qty}


# --- projection ----------------------------------------------------------------


def effective_arrival(po: dict[str, Any], commitments: dict[str, Any]) -> _dt.date:
    """When a purchase order will actually land.

    The promised date unless the supplier has said otherwise. ``commitments`` maps
    a PO id to a date extracted from correspondence — the one place in this
    calculation where a model's output is used, and it is used as a date, never as
    a judgment.
    """
    revised = commitments.get(po["po_id"])
    return parse_date(revised) if revised else parse_date(po["promised_date"])
