"""Schema for the harness itself.

Every table here would exist for any customer running Harmony. Nothing in this file
mentions a part, a purchase order or a supplier — those live in
``northfield/migrations.py``. The split is load-bearing: it is what lets the same
binary serve a different company by swapping one package.
"""

from __future__ import annotations

from harmony.kernel.store import Migration

HARMONY_MIGRATIONS: list[Migration] = [
    Migration(
        name="harmony_0001_core",
        sql="""
        -- The simulated clock's position, so a restart resumes at the same instant.
        CREATE TABLE IF NOT EXISTS clock_state (
            id  INTEGER PRIMARY KEY CHECK (id = 1),
            now TEXT NOT NULL
        );

        -- One row per pass of the agent loop.
        CREATE TABLE IF NOT EXISTS runs (
            run_id            TEXT PRIMARY KEY,
            profile_id        TEXT NOT NULL,
            principal_id      TEXT NOT NULL,
            attention_item_id TEXT,
            state             TEXT NOT NULL,
            proposal_id       TEXT,
            trigger           TEXT NOT NULL DEFAULT 'schedule',
            parent_run_id     TEXT,
            error             TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runs_state ON runs (state);
        CREATE INDEX IF NOT EXISTS idx_runs_principal ON runs (principal_id);

        -- Append-only ledger. See harmony/audit/log.py.
        CREATE TABLE IF NOT EXISTS audit_events (
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id   TEXT NOT NULL UNIQUE,
            run_id     TEXT,
            ts_clock   TEXT NOT NULL,
            ts_wall    TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            actor_id   TEXT NOT NULL,
            event_type TEXT NOT NULL,
            summary    TEXT,
            payload    TEXT NOT NULL DEFAULT '{}',
            prev_hash  TEXT NOT NULL,
            entry_hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events (run_id, seq);
        CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events (event_type);

        -- Append-only is enforced by the database, not only by convention.
        CREATE TRIGGER IF NOT EXISTS audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit_events is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit_events is append-only');
        END;
        """,
    ),
    Migration(
        name="harmony_0002_detection",
        sql="""
        -- Things a detector thinks a human should know about.
        --
        -- `fingerprint` identifies the *situation* (this part, this order) and is
        -- what dedupe keys on. `content_hash` identifies the situation's *details*;
        -- when it changes, the old item is superseded rather than silently
        -- suppressed, because "the delay got worse" is news even though "there is a
        -- delay" is not.
        CREATE TABLE IF NOT EXISTS attention_items (
            item_id       TEXT PRIMARY KEY,
            detector_id   TEXT NOT NULL,
            principal_id  TEXT NOT NULL,
            fingerprint   TEXT NOT NULL,
            content_hash  TEXT NOT NULL,
            state         TEXT NOT NULL,
            severity      TEXT NOT NULL,
            title         TEXT NOT NULL,
            subject_refs  TEXT NOT NULL DEFAULT '[]',
            evidence      TEXT NOT NULL DEFAULT '[]',
            payload       TEXT NOT NULL DEFAULT '{}',
            first_seen    TEXT NOT NULL,
            last_seen     TEXT NOT NULL,
            seen_count    INTEGER NOT NULL DEFAULT 1,
            run_id        TEXT,
            superseded_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_items_fingerprint
            ON attention_items (fingerprint, state);
        """,
    ),
    Migration(
        name="harmony_0003_planning_and_gating",
        sql="""
        -- The planner's output, stored so the approval can be bound to it.
        CREATE TABLE IF NOT EXISTS proposals (
            proposal_id  TEXT PRIMARY KEY,
            run_id       TEXT NOT NULL,
            digest       TEXT NOT NULL,
            summary      TEXT NOT NULL,
            reasoning    TEXT NOT NULL,
            evidence     TEXT NOT NULL DEFAULT '[]',
            action       TEXT NOT NULL,
            alternatives TEXT NOT NULL DEFAULT '[]',
            created_at   TEXT NOT NULL
        );

        -- `proposal_digest` binds the approval to the exact plan that was shown.
        -- If the plan changes after approval, the digest stops matching and
        -- execution refuses: approval is consent to a specific act, not a blanket.
        CREATE TABLE IF NOT EXISTS approval_requests (
            approval_id       TEXT PRIMARY KEY,
            run_id            TEXT NOT NULL,
            proposal_id       TEXT NOT NULL,
            proposal_digest   TEXT NOT NULL,
            requested_of      TEXT NOT NULL,
            originally_for    TEXT NOT NULL,
            reason            TEXT NOT NULL,
            state             TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            expires_at        TEXT NOT NULL,
            decided_at        TEXT,
            decided_by        TEXT,
            decision_note     TEXT,
            escalation_count  INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_state
            ON approval_requests (state, requested_of);
        """,
    ),
    Migration(
        name="harmony_0004_execution",
        sql="""
        -- Idempotency records make a retried write a no-op that returns the
        -- original result. This is what makes crash-resume safe: the resumed
        -- process may re-attempt the step it died inside, and gets the first
        -- result back rather than creating a second purchase order.
        CREATE TABLE IF NOT EXISTS idempotency_records (
            key        TEXT PRIMARY KEY,
            tool_name  TEXT NOT NULL,
            run_id     TEXT,
            result     TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workflow_instances (
            instance_id      TEXT PRIMARY KEY,
            run_id           TEXT NOT NULL,
            definition_name  TEXT NOT NULL,
            definition_version INTEGER NOT NULL,
            params           TEXT NOT NULL DEFAULT '{}',
            status           TEXT NOT NULL,
            cursor           INTEGER NOT NULL DEFAULT 0,
            step_results     TEXT NOT NULL DEFAULT '{}',
            compensation_log TEXT NOT NULL DEFAULT '[]',
            error            TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wf_status ON workflow_instances (status);
        """,
    ),
    Migration(
        name="harmony_0005_scheduling",
        sql="""
        -- Deferred work. Durable by construction: a task is a row, and a restarted
        -- worker finds it exactly where a running one would.
        --
        -- `dedupe_key` is UNIQUE so scheduling the same follow-up twice is a
        -- database-level no-op rather than a race the application has to win.
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            task_id     TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            payload     TEXT NOT NULL DEFAULT '{}',
            fire_at     TEXT NOT NULL,
            state       TEXT NOT NULL,
            dedupe_key  TEXT UNIQUE,
            run_id      TEXT,
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            created_at  TEXT NOT NULL,
            fired_at    TEXT,
            lease_owner TEXT,
            lease_until TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_due ON scheduled_tasks (state, fire_at);
        """,
    ),
    Migration(
        name="harmony_0006_memory",
        sql="""
        -- Durable memory holds *derived judgments*, never system-of-record state.
        -- Current facts are always re-read from the source; what persists here is
        -- the kind of belief a colleague would carry between conversations, with
        -- the provenance needed to check it and a TTL after which it must be
        -- re-derived. See DESIGN.md, "Long-term memory".
        CREATE TABLE IF NOT EXISTS memory_facts (
            fact_id       TEXT PRIMARY KEY,
            scope_kind    TEXT NOT NULL,
            scope_id      TEXT NOT NULL,
            subject       TEXT NOT NULL,
            predicate     TEXT NOT NULL,
            object        TEXT NOT NULL,
            statement     TEXT NOT NULL,
            confidence    REAL NOT NULL DEFAULT 0.5,
            provenance    TEXT NOT NULL DEFAULT '{}',
            observed_at   TEXT NOT NULL,
            expires_at    TEXT,
            state         TEXT NOT NULL DEFAULT 'active',
            superseded_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_memory_lookup
            ON memory_facts (scope_kind, scope_id, subject, state);
        """,
    ),
]
