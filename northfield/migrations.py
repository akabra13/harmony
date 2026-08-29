"""Northfield Manufacturing's systems of record.

These are *this company's* tables. Nothing in ``harmony/`` references them; the
harness reaches them only through the providers and tools in this package. A
different customer would ship a different file here and keep the whole harness.

The shapes follow the brief's sample schemas, with the deviations argued in
MODEL.md — the additions being ``goods_receipts`` (without which the Tuesday
arrival check would be simulated rather than real), ``on_time_rate`` on suppliers
(so the supplier choice has a basis beyond price), and ``shortage_flags`` (the
cross-team escalation Scenario B needs when no lot can cover).
"""

from __future__ import annotations

from harmony.kernel.store import Migration

NORTHFIELD_MIGRATIONS: list[Migration] = [
    Migration(
        name="northfield_0001_people",
        sql="""
        -- The directory. In a real deployment this is Entra ID and the harness
        -- reads it through a connector; the interface (harmony/identity/directory.py)
        -- is what would not change.
        CREATE TABLE IF NOT EXISTS users (
            user_id            TEXT PRIMARY KEY,
            name               TEXT NOT NULL,
            email              TEXT NOT NULL,
            role               TEXT NOT NULL,
            manager_id         TEXT,
            backup_approver_id TEXT,
            scopes             TEXT NOT NULL DEFAULT '[]',
            approval_limits    TEXT NOT NULL DEFAULT '{}'
        );
        """,
    ),
    Migration(
        name="northfield_0002_erp",
        sql="""
        CREATE TABLE IF NOT EXISTS parts (
            part_id      TEXT PRIMARY KEY,
            description  TEXT NOT NULL,
            on_hand      INTEGER NOT NULL DEFAULT 0,
            daily_usage  REAL NOT NULL DEFAULT 0,
            safety_stock INTEGER NOT NULL DEFAULT 0,
            unit_cost    REAL NOT NULL DEFAULT 0,
            lot_tracked  INTEGER NOT NULL DEFAULT 0
        );

        -- `approved_parts` is the qualification list: being an approved *vendor*
        -- and being approved for a *part* are different things, and conflating them
        -- is precisely the mistake the Apex Rapid Supply record is here to catch.
        CREATE TABLE IF NOT EXISTS suppliers (
            supplier_id     TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            contact_email   TEXT NOT NULL,
            approved        INTEGER NOT NULL DEFAULT 0,
            approved_parts  TEXT NOT NULL DEFAULT '[]',
            lead_time_days  INTEGER NOT NULL DEFAULT 0,
            pricing         TEXT NOT NULL DEFAULT '{}',
            on_time_rate    REAL NOT NULL DEFAULT 1.0
        );

        CREATE TABLE IF NOT EXISTS purchase_orders (
            po_id         TEXT PRIMARY KEY,
            part_id       TEXT NOT NULL,
            supplier_id   TEXT NOT NULL,
            qty           INTEGER NOT NULL,
            unit_price    REAL NOT NULL,
            total_value   REAL NOT NULL,
            ordered_date  TEXT NOT NULL,
            promised_date TEXT NOT NULL,
            status        TEXT NOT NULL,
            created_by    TEXT,
            notes         TEXT,
            replaces_po   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_po_part ON purchase_orders (part_id, status);

        CREATE TABLE IF NOT EXISTS production_orders (
            prod_order_id  TEXT PRIMARY KEY,
            product        TEXT NOT NULL,
            qty            INTEGER NOT NULL,
            scheduled_start TEXT NOT NULL,
            scheduled_end  TEXT NOT NULL,
            status         TEXT NOT NULL,
            line           TEXT NOT NULL,
            supervisor_id  TEXT NOT NULL,
            components     TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_prod_start ON production_orders (scheduled_start);

        -- Receipts make arrival a fact rather than an assumption. The Tuesday
        -- follow-up asks this table whether the shipment landed; without it the
        -- check would have nothing to check.
        CREATE TABLE IF NOT EXISTS goods_receipts (
            receipt_id    TEXT PRIMARY KEY,
            po_id         TEXT NOT NULL,
            part_id       TEXT NOT NULL,
            qty           INTEGER NOT NULL,
            received_date TEXT NOT NULL,
            received_by   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_receipts_po ON goods_receipts (po_id);
        """,
    ),
    Migration(
        name="northfield_0003_quality",
        sql="""
        -- Lot tracking and quality holds. Added for Scenario B; note that adding it
        -- required no change to the harness, only new rows here and new plugins in
        -- this package.
        CREATE TABLE IF NOT EXISTS quality_lots (
            lot_id         TEXT PRIMARY KEY,
            part_id        TEXT NOT NULL,
            qty            INTEGER NOT NULL,
            status         TEXT NOT NULL,
            received_date  TEXT NOT NULL,
            allocated_to   TEXT NOT NULL DEFAULT '[]',
            hold_reason    TEXT,
            hold_placed_by TEXT,
            hold_placed_on TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_lots_part ON quality_lots (part_id, status);

        -- Quality raising a shortage with purchasing. A hand-off between two
        -- people's agents, which is why it is a record rather than an email.
        CREATE TABLE IF NOT EXISTS shortage_flags (
            flag_id       TEXT PRIMARY KEY,
            part_id       TEXT NOT NULL,
            prod_order_id TEXT NOT NULL,
            qty_short     INTEGER NOT NULL,
            needed_by     TEXT NOT NULL,
            raised_by     TEXT NOT NULL,
            raised_on     TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'open',
            note          TEXT
        );
        """,
    ),
    Migration(
        name="northfield_0004_communications",
        sql="""
        -- One table for inbound and outbound. A notification the agent sends is
        -- the same kind of object as an email it read, and giving them separate
        -- tables would mean the audit could not show a thread.
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            direction  TEXT NOT NULL DEFAULT 'inbound',
            from_addr  TEXT NOT NULL,
            to_addrs   TEXT NOT NULL DEFAULT '[]',
            date       TEXT NOT NULL,
            subject    TEXT NOT NULL,
            body       TEXT NOT NULL,
            thread_id  TEXT,
            sent_by    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_date ON messages (date);

        CREATE TABLE IF NOT EXISTS calendar_events (
            event_id       TEXT PRIMARY KEY,
            owner          TEXT NOT NULL,
            start          TEXT NOT NULL,
            end            TEXT NOT NULL,
            title          TEXT NOT NULL,
            attendees      TEXT NOT NULL DEFAULT '[]',
            out_of_office  INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_events_owner ON calendar_events (owner, start);

        -- Notifications to production. A record rather than a side effect, so that
        -- "was the supervisor told?" is answerable from the system rather than only
        -- from the audit log.
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id TEXT PRIMARY KEY,
            recipient_id    TEXT NOT NULL,
            subject         TEXT NOT NULL,
            body            TEXT NOT NULL,
            sent_on         TEXT NOT NULL,
            sent_by         TEXT NOT NULL,
            about           TEXT
        );
        """,
    ),
]
