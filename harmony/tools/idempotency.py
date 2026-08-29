"""Idempotency: making "did it happen?" a question with an answer.

A distributed system's hardest moment is the one after a crash, when the process
knows it was about to do something but not whether it did. The harness answers that
by deriving a deterministic key for every write and recording the result under it.
Re-attempting a call with the same key returns the first result instead of acting
again.

The key is ``sha256(run_id, step_id, tool, canonical(params))``:

* ``run_id`` — two runs proposing the same reroute are two real decisions.
* ``step_id`` — a workflow's step 4 is a different act from its step 6, even with
  identical arguments.
* ``params`` — changing an argument makes it a new call, not a retry.

Every part is deterministic, so a resumed process computes the same key the dead
one did. Nothing here depends on a random id or a timestamp; that is the property
that makes resume safe.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from harmony.kernel.ids import digest
from harmony.kernel.store import Store, dump_json, load_json


def idempotency_key(*, run_id: str, step_id: str, tool: str, params: dict[str, Any]) -> str:
    """Derive the key for one logical write."""
    return digest(run_id, step_id, tool, params)


class IdempotencyStore:
    """Records results by key so a repeated write becomes a replay."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def lookup(self, key: str) -> dict[str, Any] | None:
        row = self._store.query_one(
            "SELECT result FROM idempotency_records WHERE key = ?", (key,)
        )
        return load_json(row["result"]) if row else None

    def record(
        self,
        *,
        key: str,
        tool: str,
        run_id: str | None,
        result: dict[str, Any],
        now: _dt.datetime,
    ) -> None:
        """Store a result. Uses INSERT OR IGNORE so that a race between two
        attempts leaves the first result in place — the winner is arbitrary but the
        outcome is single-valued, which is what callers depend on."""
        self._store.execute(
            """
            INSERT OR IGNORE INTO idempotency_records (key, tool_name, run_id, result, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, tool, run_id, dump_json(result), now.isoformat()),
        )
