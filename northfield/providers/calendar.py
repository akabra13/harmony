"""The calendar provider, and the availability oracle behind approval escalation.

The provider gives a run its own calendar. The oracle answers one narrow question
about *someone else's* — "is this person out on this day?" — and it is deliberately
a different code path with a different identity behind it.

That separation matters. Reading a colleague's diary is not something a purchasing
manager needs rights to just because the harness wants to route an approval, so the
oracle runs under the system principal with free/busy access only. It never returns
a title, an attendee list, or anything but a boolean.
"""

from __future__ import annotations

import datetime as _dt

from harmony.identity.session import Session
from harmony.providers.base import ContextRequest, ContextSlice, Redaction, provider
from northfield.systems import calendar


@provider(
    "calendar",
    description="This user's own calendar, including out-of-office periods.",
    required_scopes={"calendar:read"},
)
def calendar_provider(session: Session, request: ContextRequest) -> ContextSlice:
    store = session.services.store  # type: ignore[union-attr]
    slice_ = ContextSlice(system="calendar", provider="northfield.calendar")

    own = calendar.events_for(store, session.principal.id)
    slice_.collections["events"] = own

    # Subjects may name other people — an approver, a supervisor. Their diaries are
    # not this user's to read, and saying so is more useful than omitting them.
    others = [uid for uid in request.subjects_of("user") if uid != session.principal.id]
    if others:
        slice_.redactions.append(
            Redaction(
                collection="events",
                count=len(others),
                reason=(
                    f"calendars of {others} belong to other people; availability is "
                    "checked separately under a free/busy-only system identity"
                ),
            )
        )
    return slice_


class CalendarAvailability:
    """Answers whether someone is available on a day.

    Implements :class:`harmony.gate.approvals.AvailabilityOracle`. Swapping this for
    a Microsoft Graph implementation is the whole of what "connect a real calendar"
    means here — nothing in the escalation logic knows the difference.
    """

    FREEBUSY_SCOPE = "calendar:freebusy:read"

    def __init__(self, harness) -> None:
        self._harness = harness

    def is_available(self, session: Session, user_id: str, day: _dt.date) -> bool:
        session.require(
            self.FREEBUSY_SCOPE, subject=f"check whether {user_id} is available on {day}"
        )
        store = self._harness.store
        return not calendar.is_out_of_office(store, user_id, day)


def availability_factory(harness) -> CalendarAvailability:
    return CalendarAvailability(harness)
