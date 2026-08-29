"""SQLite persistence: connection management, reentrant transactions, migrations.

The harness and the company each own a migration set. They are applied to the same
database but registered separately, which keeps the kernel/instance split visible
all the way down to the schema: ``harmony`` ships tables that exist for every
customer (runs, audit, approvals, workflow instances), while ``northfield`` ships
tables that describe one manufacturer's systems of record.

Transactions are reentrant. A tool invocation opens one; the workflow engine has
already opened one around "run the step and advance the cursor". Nesting uses
SAVEPOINTs so the inner block can fail without discarding the outer one.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Migration:
    """One forward-only schema change, applied once and recorded by name."""

    name: str
    sql: str


class Store:
    """Owns the database connection and the transaction stack.

    A single connection is used deliberately. The harness is a one-process design
    (see README, "what I cut"); the seam that would let it become many processes is
    the durable task queue, not a connection pool.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._depth = 0
        self._ensure_migration_table()

    # --- lifecycle -------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def _ensure_migration_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

    def migrate(self, migrations: Sequence[Migration]) -> list[str]:
        """Apply any migrations not yet recorded. Returns the names applied.

        ``executescript`` issues an implicit COMMIT before running, so migrations
        cannot sit inside our transaction helper. Each statement is therefore
        applied on its own and the migration is recorded only once every statement
        succeeded — a partial application is left unrecorded and retried on the
        next start, which is safe because every statement is ``IF NOT EXISTS``.
        """
        applied: list[str] = []
        seen = {row["name"] for row in self._conn.execute("SELECT name FROM schema_migrations")}
        for migration in migrations:
            if migration.name in seen:
                continue
            self._conn.executescript(migration.sql)
            self._conn.execute(
                "INSERT INTO schema_migrations (name) VALUES (?)", (migration.name,)
            )
            applied.append(migration.name)
        return applied

    # --- transactions ----------------------------------------------------------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Reentrant transaction. Outermost commits; nested levels use savepoints."""
        if self._depth == 0:
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                self._depth = 0
                raise
            else:
                self._conn.execute("COMMIT")
                self._depth = 0
        else:
            name = f"sp_{self._depth}"
            self._conn.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._conn.execute(f"ROLLBACK TO {name}")
                self._conn.execute(f"RELEASE {name}")
                self._depth -= 1
                raise
            else:
                self._conn.execute(f"RELEASE {name}")
                self._depth -= 1

    # --- query helpers ---------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        cursor = self._conn.execute(sql, params)
        return cursor.fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self.tx() as conn:
            return conn.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self.tx() as conn:
            conn.executemany(sql, rows)


# --- JSON column helpers -------------------------------------------------------
#
# SQLite has no JSON type. Rather than scatter json.dumps/loads through every
# repository, all structured columns go through these two functions.


def dump_json(value: Any) -> str:
    return json.dumps(value, default=str)


def load_json(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)
