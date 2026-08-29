"""The calendar connector.

Stands in for Graph's calendar endpoints. :func:`is_out_of_office` is the only
question the harness's escalation rule actually asks, and it asks it about one
person on one day — which is why the availability oracle it backs needs only
free/busy access rather than the contents of anyone's diary.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from harmony.kernel.store import Store, load_json


def events_for(
    store: Store, owner: str, *, day: _dt.date | None = None
) -> list[dict[str, Any]]:
    """Events owned by a person: all of them, or only those covering one day.

    There is deliberately no "next N days" variant that defaults to today. Anything
    needing a window takes explicit dates, because a connector that reached for the
    wall clock would be immune to advancing the simulated one — and the follow-up
    is the exact feature that would then break silently.
    """
    rows = store.query(
        "SELECT * FROM calendar_events WHERE owner = ? ORDER BY start", (owner,)
    )
    events = [_event(r) for r in rows]
    return events if day is None else [e for e in events if _covers(e, day)]


def is_out_of_office(store: Store, owner: str, day: _dt.date) -> bool:
    """Whether this person is away on this day.

    Away, not busy. A packed diary is not absence, and treating it as such would
    route approvals away from people sitting at their desks — which is why the
    seed data includes a normal meeting on the day in question.
    """
    return any(e["out_of_office"] and _covers(e, day) for e in events_for(store, owner, day=day))


def _covers(event: dict[str, Any], day: _dt.date) -> bool:
    start = _dt.datetime.fromisoformat(event["start"]).date()
    end = _dt.datetime.fromisoformat(event["end"]).date()
    return start <= day <= end


def _event(row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "owner": row["owner"],
        "start": row["start"],
        "end": row["end"],
        "title": row["title"],
        "attendees": load_json(row["attendees"], []),
        "out_of_office": bool(row["out_of_office"]),
    }
