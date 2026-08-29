"""The mail connector.

Stands in for Microsoft Graph. The interesting method is :func:`inbox_for`, which
returns only messages the named person is actually a party to — the mailbox
equivalent of a delegated ``Mail.Read`` token, and the reason the provider can
report a redaction count rather than silently filtering.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from harmony.kernel.store import Store, dump_json, load_json


def inbox_for(
    store: Store, email: str, *, since: _dt.date | None = None, limit: int = 50
) -> tuple[list[dict[str, Any]], int]:
    """Messages this person sent or received, and how many were withheld.

    The second element is what makes the audit honest. "Read 6 messages" and "read
    6 of 7 messages, one withheld because you are not a party to it" are different
    statements, and only the second lets a reviewer tell an oversight from a
    permission boundary.
    """
    rows = store.query("SELECT * FROM messages ORDER BY date DESC")
    visible: list[dict[str, Any]] = []
    withheld = 0

    for row in rows:
        message = _message(row)
        if since and message["date"][:10] < since.isoformat():
            continue
        if email == message["from_addr"] or email in message["to_addrs"]:
            visible.append(message)
        else:
            withheld += 1

    return visible[:limit], withheld


def send(
    store: Store,
    *,
    message_id: str,
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body: str,
    sent_on: _dt.datetime,
    sent_by: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    store.execute(
        """
        INSERT INTO messages (
            message_id, direction, from_addr, to_addrs, date, subject, body,
            thread_id, sent_by
        ) VALUES (?, 'outbound', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            from_addr,
            dump_json(to_addrs),
            sent_on.isoformat(),
            subject,
            body,
            thread_id,
            sent_by,
        ),
    )
    return {"message_id": message_id, "to": to_addrs, "subject": subject}


def get(store: Store, message_id: str) -> dict[str, Any] | None:
    row = store.query_one("SELECT * FROM messages WHERE message_id = ?", (message_id,))
    return _message(row) if row else None


def delete(store: Store, message_id: str) -> bool:
    """Used only to compensate a notification that has not yet been read.

    Present so that the workflow's irreversibility argument is made honestly: this
    exists, it is not wired into the reroute workflow, and MODEL.md explains why —
    unsending a message a person has already seen is not something a database can do.
    """
    store.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))
    return get(store, message_id) is None


def _message(row) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "direction": row["direction"],
        "from_addr": row["from_addr"],
        "to_addrs": load_json(row["to_addrs"], []),
        "date": row["date"],
        "subject": row["subject"],
        "body": row["body"],
        "thread_id": row["thread_id"],
    }
