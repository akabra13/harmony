"""The quality connector: lot status, allocations, holds, and shortage flags.

Added for Scenario B. Worth noting what adding it did *not* require: no change to
the context broker, the tool invoker, the gate, the planner or the audit layer. A
new system of record is a new connector, a new provider and some new tools —
which is the claim the kernel/company split makes, tested here by actually doing it.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from harmony.kernel.store import Store, dump_json, load_json


def get_lot(store: Store, lot_id: str) -> dict[str, Any] | None:
    row = store.query_one("SELECT * FROM quality_lots WHERE lot_id = ?", (lot_id,))
    return _lot(row) if row else None


def lots_for_part(store: Store, part_id: str) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT * FROM quality_lots WHERE part_id = ? ORDER BY received_date", (part_id,)
    )
    return [_lot(r) for r in rows]


def lots_on_hold(store: Store) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT * FROM quality_lots WHERE status = 'hold' ORDER BY hold_placed_on"
    )
    return [_lot(r) for r in rows]


def available_lots_for_part(
    store: Store, part_id: str, *, min_qty: int = 0
) -> list[dict[str, Any]]:
    """Released, unallocated lots of a part, largest first.

    Three conditions, and the seed data has a distractor for each: L-2065 is
    scrapped rather than released, L-2088 is released but already committed to
    another order, and L-2077 is available but a different part.
    """
    return [
        lot
        for lot in lots_for_part(store, part_id)
        if lot["status"] == "released"
        and not lot["allocated_to"]
        and lot["qty"] >= min_qty
    ]


def set_lot_status(
    store: Store,
    lot_id: str,
    status: str,
    *,
    reason: str | None = None,
    placed_by: str | None = None,
    placed_on: _dt.date | None = None,
) -> dict[str, Any] | None:
    store.execute(
        """
        UPDATE quality_lots
        SET status = ?, hold_reason = ?, hold_placed_by = ?, hold_placed_on = ?
        WHERE lot_id = ?
        """,
        (
            status,
            reason,
            placed_by,
            placed_on.isoformat() if placed_on else None,
            lot_id,
        ),
    )
    return get_lot(store, lot_id)


def set_allocation(store: Store, lot_id: str, allocated_to: list[str]) -> dict[str, Any] | None:
    store.execute(
        "UPDATE quality_lots SET allocated_to = ? WHERE lot_id = ?",
        (dump_json(allocated_to), lot_id),
    )
    return get_lot(store, lot_id)


def _lot(row) -> dict[str, Any]:
    return {
        "lot_id": row["lot_id"],
        "part_id": row["part_id"],
        "qty": row["qty"],
        "status": row["status"],
        "received_date": row["received_date"],
        "allocated_to": load_json(row["allocated_to"], []),
        "hold_reason": row["hold_reason"],
        "hold_placed_by": row["hold_placed_by"],
        "hold_placed_on": row["hold_placed_on"],
    }


# --- shortage flags ------------------------------------------------------------


def raise_shortage_flag(
    store: Store,
    *,
    flag_id: str,
    part_id: str,
    prod_order_id: str,
    qty_short: int,
    needed_by: _dt.date,
    raised_by: str,
    raised_on: _dt.date,
    note: str = "",
) -> dict[str, Any]:
    """Hand a material problem to purchasing.

    A record rather than an email, because the receiving team's agent should be
    able to detect it. This is where one person's agent creates work for another's.
    """
    store.execute(
        """
        INSERT INTO shortage_flags (
            flag_id, part_id, prod_order_id, qty_short, needed_by,
            raised_by, raised_on, status, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            flag_id,
            part_id,
            prod_order_id,
            qty_short,
            needed_by.isoformat(),
            raised_by,
            raised_on.isoformat(),
            note,
        ),
    )
    return get_shortage_flag(store, flag_id)  # type: ignore[return-value]


def get_shortage_flag(store: Store, flag_id: str) -> dict[str, Any] | None:
    row = store.query_one("SELECT * FROM shortage_flags WHERE flag_id = ?", (flag_id,))
    return dict(row) if row else None


def withdraw_shortage_flag(store: Store, flag_id: str) -> bool:
    store.execute(
        "UPDATE shortage_flags SET status = 'withdrawn' WHERE flag_id = ?", (flag_id,)
    )
    flag = get_shortage_flag(store, flag_id)
    return bool(flag and flag["status"] == "withdrawn")


def open_shortage_flags(store: Store) -> list[dict[str, Any]]:
    rows = store.query("SELECT * FROM shortage_flags WHERE status = 'open' ORDER BY raised_on")
    return [dict(r) for r in rows]


# --- notifications -------------------------------------------------------------


def record_notification(
    store: Store,
    *,
    notification_id: str,
    recipient_id: str,
    subject: str,
    body: str,
    sent_on: _dt.datetime,
    sent_by: str,
    about: str = "",
) -> dict[str, Any]:
    store.execute(
        """
        INSERT INTO notifications (
            notification_id, recipient_id, subject, body, sent_on, sent_by, about
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (notification_id, recipient_id, subject, body, sent_on.isoformat(), sent_by, about),
    )
    return {"notification_id": notification_id, "recipient_id": recipient_id}


def notifications_about(store: Store, about: str) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT * FROM notifications WHERE about = ? ORDER BY sent_on", (about,)
    )
    return [dict(r) for r in rows]
