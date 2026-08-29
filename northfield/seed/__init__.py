"""Loading Northfield's seed data into the systems of record.

In a real deployment there is nothing here: the ERP already contains the purchase
orders and the connectors point at it. This module stands in for that, and is
deliberately the only place in the package that writes to the systems of record
outside a tool — which keeps the "every change goes through the tool invoker"
invariant true of everything that happens after start-up.

Seeding is idempotent, so rebuilding a database over an existing one restores the
starting position rather than failing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from harmony.kernel.store import Store, dump_json

SEED_DIR = Path(__file__).parent

# (file, table, columns). Columns holding structured values are JSON-encoded on
# the way in; `_JSON_COLUMNS` says which.
_TABLES: list[tuple[str, str, list[str]]] = [
    (
        "users.yaml",
        "users",
        [
            "user_id",
            "name",
            "email",
            "role",
            "manager_id",
            "backup_approver_id",
            "scopes",
            "approval_limits",
        ],
    ),
    (
        "parts.yaml",
        "parts",
        [
            "part_id",
            "description",
            "on_hand",
            "daily_usage",
            "safety_stock",
            "unit_cost",
            "lot_tracked",
        ],
    ),
    (
        "suppliers.yaml",
        "suppliers",
        [
            "supplier_id",
            "name",
            "contact_email",
            "approved",
            "approved_parts",
            "lead_time_days",
            "pricing",
            "on_time_rate",
        ],
    ),
    (
        "purchase_orders.yaml",
        "purchase_orders",
        [
            "po_id",
            "part_id",
            "supplier_id",
            "qty",
            "unit_price",
            "total_value",
            "ordered_date",
            "promised_date",
            "status",
            "created_by",
        ],
    ),
    (
        "production_orders.yaml",
        "production_orders",
        [
            "prod_order_id",
            "product",
            "qty",
            "scheduled_start",
            "scheduled_end",
            "status",
            "line",
            "supervisor_id",
            "components",
        ],
    ),
    (
        "quality_lots.yaml",
        "quality_lots",
        [
            "lot_id",
            "part_id",
            "qty",
            "status",
            "received_date",
            "allocated_to",
            "hold_reason",
            "hold_placed_by",
            "hold_placed_on",
        ],
    ),
    (
        "goods_receipts.yaml",
        "goods_receipts",
        ["receipt_id", "po_id", "part_id", "qty", "received_date", "received_by"],
    ),
    (
        "messages.yaml",
        "messages",
        [
            "message_id",
            "direction",
            "from_addr",
            "to_addrs",
            "date",
            "subject",
            "body",
            "thread_id",
        ],
    ),
    (
        "calendar_events.yaml",
        "calendar_events",
        ["event_id", "owner", "start", "end", "title", "attendees", "out_of_office"],
    ),
]

_JSON_COLUMNS = {
    "scopes",
    "approval_limits",
    "approved_parts",
    "pricing",
    "components",
    "allocated_to",
    "to_addrs",
    "attendees",
}


def seed(store: Store) -> dict[str, int]:
    """Load every seed file. Returns rows written per table."""
    written: dict[str, int] = {}
    for filename, table, columns in _TABLES:
        rows = yaml.safe_load((SEED_DIR / filename).read_text(encoding="utf-8")) or []
        values = [tuple(_encode(col, row.get(col)) for col in columns) for row in rows]
        placeholders = ", ".join("?" for _ in columns)
        store.executemany(
            f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        written[table] = len(values)
    return written


def _encode(column: str, value: Any) -> Any:
    if column in _JSON_COLUMNS:
        return dump_json(value if value is not None else [])
    if isinstance(value, bool):
        return int(value)
    return value
