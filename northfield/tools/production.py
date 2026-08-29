"""Telling production something.

One tool, and the interesting thing about it is that it declares no compensation.

That is not an oversight, and the loader would reject it if it were: a write with
neither a compensation nor ``irreversible: true`` fails validation. Sending a
message to a supervisor is genuinely irreversible — the ``notifications`` table
could be emptied, but the person has read it, and a rollback that pretends
otherwise makes the audit trail lie.

The consequence shapes the reroute workflow. Because notifying cannot be undone, it
is ordered *after* every write that can be, so a failure late in the sequence rolls
back cleanly and a failure after the notification is at least a failure the
supervisor already knows about. Ordering steps by reversibility is a property of
declared workflows worth more than it looks; DESIGN.md argues the case.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from harmony.identity.session import Session
from harmony.kernel.errors import ToolFailed
from harmony.tools.base import tool
from northfield.systems import quality


class NotifySupervisorInput(BaseModel):
    supervisor_id: str = Field(description="The production supervisor to notify.")
    subject: str = Field(max_length=200)
    body: str = Field(max_length=4000)
    about: str = Field(default="", description="Production order or PO this concerns.")


class NotifySupervisorOutput(BaseModel):
    notification_id: str
    recipient_id: str
    delivered: bool = True


@tool(
    "production.notify_supervisor",
    description=(
        "Send a production supervisor a message about a change affecting their line. "
        "This cannot be retracted once sent."
    ),
    scopes={"production:notify"},
    input=NotifySupervisorInput,
    output=NotifySupervisorOutput,
    writes=True,
    compensation=None,  # irreversible by nature; see the module docstring
    system="production",
)
def notify_supervisor(
    session: Session, inp: NotifySupervisorInput
) -> NotifySupervisorOutput:
    store = session.services.store  # type: ignore[union-attr]
    row = store.query_one("SELECT user_id FROM users WHERE user_id = ?", (inp.supervisor_id,))
    if row is None:
        raise ToolFailed(f"no such recipient '{inp.supervisor_id}'")

    result = quality.record_notification(
        store,
        notification_id=session.derive_id("NTF", 4),
        recipient_id=inp.supervisor_id,
        subject=inp.subject,
        body=inp.body,
        sent_on=session.clock.now(),
        sent_by=session.principal.id,
        about=inp.about,
    )
    return NotifySupervisorOutput(**result)
