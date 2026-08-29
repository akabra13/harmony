"""Time, as an injected dependency.

Nothing in the harness may call ``datetime.now()``. Every timestamp, every "is this
due?" comparison, and every scheduling decision reads from a :class:`Clock` handed
in at construction. The demo depends on this: advancing to Tuesday must fire the
follow-up exactly as the real passage of time would.

``tests/architecture/test_no_wall_clock.py`` enforces the rule.
"""

from __future__ import annotations

import datetime as _dt
from typing import Protocol, runtime_checkable

# The one module allowed to touch the real clock. See the architecture test.
_WALL = _dt.datetime


@runtime_checkable
class Clock(Protocol):
    """Source of truth for 'now' within a run."""

    def now(self) -> _dt.datetime:
        """Current instant, naive and interpreted as site-local time."""

    def today(self) -> _dt.date:
        """Current date."""

    def end_of_day(self, day: _dt.date | None = None) -> _dt.datetime:
        """Last instant of ``day`` (default: today). Used for approval expiry."""


class _ClockBase:
    def today(self) -> _dt.date:
        return self.now().date()  # type: ignore[attr-defined]

    def end_of_day(self, day: _dt.date | None = None) -> _dt.datetime:
        day = day or self.today()
        return _dt.datetime.combine(day, _dt.time(23, 59, 59))


class SystemClock(_ClockBase):
    """Wall-clock time. What a real deployment runs on."""

    def now(self) -> _dt.datetime:
        return _WALL.now()


class SimulatedClock(_ClockBase):
    """An advanceable clock whose position is durable.

    The position is persisted so a restarted process resumes at the same simulated
    instant. Time only ever moves forward; attempting to rewind raises, because a
    backwards clock would let already-fired scheduled tasks come due again.
    """

    def __init__(self, start: _dt.datetime, on_change=None) -> None:
        self._now = start
        self._on_change = on_change

    def now(self) -> _dt.datetime:
        return self._now

    def advance_to(self, target: _dt.datetime | _dt.date) -> _dt.datetime:
        """Move the clock forward to ``target``. Returns the new instant."""
        if isinstance(target, _dt.date) and not isinstance(target, _dt.datetime):
            target = _dt.datetime.combine(target, _dt.time(9, 0, 0))
        if target < self._now:
            raise ValueError(
                f"clock cannot move backwards: now={self._now.isoformat()} "
                f"target={target.isoformat()}"
            )
        self._now = target
        if self._on_change:
            self._on_change(target)
        return self._now

    def advance_by(self, **delta: float) -> _dt.datetime:
        """Move forward by a ``timedelta`` keyword spec, e.g. ``advance_by(days=1)``."""
        return self.advance_to(self._now + _dt.timedelta(**delta))


def parse_date(value: str | _dt.date | _dt.datetime) -> _dt.date:
    """Coerce seed/config values into a date without pulling in a parser dependency."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value)[:10])


def parse_datetime(value: str | _dt.date | _dt.datetime) -> _dt.datetime:
    """Coerce seed/config values into a datetime, defaulting bare dates to midnight."""
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time.min)
    text = str(value).strip().replace("Z", "")
    if len(text) <= 10:
        return _dt.datetime.combine(_dt.date.fromisoformat(text), _dt.time.min)
    return _dt.datetime.fromisoformat(text)
