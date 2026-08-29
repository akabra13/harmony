"""The mail provider — and the one place a model reads prose on the agent's behalf.

Kestrel's email says *"Revised ship date is Monday 9/7, which puts it on your dock
Tuesday 9/8."* No amount of parsing gets 2026-09-08 out of that reliably. This is
the part of the problem a language model is genuinely better at, and so it is the
part a language model does.

The boundary is drawn tightly around it:

* The model's input is one email body and the list of purchase orders that could
  plausibly be its subject.
* Its output is a :class:`SupplierCommitment` — a purchase order id, a date, a
  confidence, and the verbatim sentence the date came from. Nothing else.
* The purchase order id is checked against the ones actually supplied, so the model
  cannot invent a PO or attach a date to the wrong one.
* The verbatim quote is carried all the way into the attention item's evidence, so
  a human reviewing the alert can check the extraction against the source sentence
  rather than trusting it.

Everything downstream — days of cover, whether the arrival misses the production
start, whether that warrants a reroute — is arithmetic over a typed date. The model
reads the letter; it does not do the sums, and it does not decide anything.
"""

from __future__ import annotations

import datetime as _dt

from pydantic import BaseModel, Field

from harmony.identity.session import Session
from harmony.kernel.errors import LLMOutputInvalid
from harmony.llm.structured import ask
from harmony.providers.base import ContextRequest, ContextSlice, Redaction, provider
from northfield.systems import erp, mail

EXTRACTION_SYSTEM = """\
You extract delivery commitments from supplier correspondence. You do not judge \
whether a delay matters, you do not recommend anything, and you do not perform \
calculations. Read the message and report what the supplier committed to.

If the message revises a delivery date for one of the listed purchase orders, \
report that order's id and the revised arrival date at our site. Watch the \
distinction between a ship date and an arrival date: if the sender gives both, \
the arrival date is the one we need.

If the message confirms an existing date, revises nothing, or concerns no listed \
purchase order, set revises_delivery to false and leave the other fields empty. \
Confirming that a shipment is on schedule is not a revision.

Quote the exact sentence your date came from, verbatim."""


class SupplierCommitment(BaseModel):
    """What a supplier said about when something will arrive."""

    revises_delivery: bool = Field(
        description="True only if this message changes a delivery date for a listed PO."
    )
    po_id: str | None = Field(
        default=None, description="The purchase order affected. Must be one of those listed."
    )
    revised_arrival_date: _dt.date | None = Field(
        default=None, description="When the goods will reach our site, as an ISO date."
    )
    confidence: float = Field(
        default=0.0, description="0 to 1: how certain the message makes this."
    )
    verbatim_quote: str | None = Field(
        default=None, description="The exact sentence the date came from."
    )


@provider(
    "mail",
    description="Correspondence this user is a party to, with supplier commitments extracted.",
    required_scopes={"mail:read"},
)
def mail_provider(session: Session, request: ContextRequest) -> ContextSlice:
    """Return the user's relevant mail, plus any delivery commitments in it."""
    store = session.services.store  # type: ignore[union-attr]
    messages, withheld = mail.inbox_for(
        store, session.principal.email, since=request.since
    )

    slice_ = ContextSlice(system="mail", provider="northfield.mail")
    slice_.collections["messages"] = messages
    if withheld:
        slice_.redactions.append(
            Redaction(
                collection="messages",
                count=withheld,
                reason=f"{session.principal.label} is not a party to these messages",
            )
        )

    candidate_pos = _candidate_purchase_orders(store, request)
    if candidate_pos:
        commitments = _extract_commitments(session, messages, candidate_pos)
        slice_.collections["supplier_commitments"] = commitments
        slice_.notes.append(
            f"extracted {len(commitments)} delivery commitment(s) from "
            f"{len(messages)} message(s)"
        )

    return slice_


def _candidate_purchase_orders(store, request: ContextRequest) -> list[dict]:
    """Which purchase orders an email could plausibly be about.

    Constrained rather than open: the model is shown the orders relevant to this
    run, so a hallucinated id fails validation instead of quietly attaching a date
    to something real but unrelated.
    """
    named = request.subjects_of("purchase_order")
    if named:
        return [po for po in (erp.get_purchase_order(store, p) for p in named) if po]
    return erp.open_purchase_orders(store)


def _extract_commitments(
    session: Session, messages: list[dict], candidates: list[dict]
) -> list[dict]:
    """Run extraction over each message, skipping ones that cannot be relevant."""
    allowed_ids = {po["po_id"] for po in candidates}
    summary = [
        {
            "po_id": po["po_id"],
            "part_id": po["part_id"],
            "supplier_id": po["supplier_id"],
            "promised_date": po["promised_date"],
        }
        for po in candidates
    ]

    commitments: list[dict] = []
    for message in messages:
        if message["direction"] != "inbound" or not _might_concern(message, allowed_ids):
            continue

        try:
            extracted = ask(
                session.services.llm,  # type: ignore[union-attr]
                session,
                call_site="mail.extract_commitment",
                system=EXTRACTION_SYSTEM,
                prompt=_extraction_prompt(message, summary),
                output_model=SupplierCommitment,
                guardrails=[_po_must_be_listed(allowed_ids)],
                max_tokens=700,
            )
        except LLMOutputInvalid:
            # Extraction that cannot be trusted is dropped, not guessed at. The
            # rejection is already in the audit; the detector proceeds on the
            # promised dates, which is the conservative reading.
            continue

        if extracted.revises_delivery and extracted.po_id and extracted.revised_arrival_date:
            commitments.append(
                {
                    "po_id": extracted.po_id,
                    "revised_arrival_date": extracted.revised_arrival_date.isoformat(),
                    "confidence": extracted.confidence,
                    "source_message_id": message["message_id"],
                    "verbatim_quote": extracted.verbatim_quote,
                }
            )
    return commitments


def _might_concern(message: dict, allowed_ids: set[str]) -> bool:
    """A cheap filter before an expensive call.

    A message naming a purchase order, or on a thread named after one, is worth
    reading. The newsletter and the meeting reschedule are not, and skipping them
    keeps the token cost of a detection sweep proportional to the mail that could
    possibly matter.
    """
    haystack = f"{message['subject']} {message['body']} {message['thread_id'] or ''}"
    return any(po_id in haystack for po_id in allowed_ids) or any(
        po_id.split("-")[-1] in haystack for po_id in allowed_ids
    )


def _extraction_prompt(message: dict, candidates: list[dict]) -> str:
    import json

    return f"""\
## Open purchase orders this message could concern

{json.dumps(candidates, indent=2)}

## Message

From: {message['from_addr']}
Date: {message['date']}
Subject: {message['subject']}

{message['body']}"""


def _po_must_be_listed(allowed: set[str]):
    """Reject a commitment attached to a purchase order that was not offered."""

    def check(output: SupplierCommitment) -> None:
        if output.revises_delivery and output.po_id not in allowed:
            raise LLMOutputInvalid(
                f"po_id {output.po_id!r} is not one of the purchase orders supplied",
                po_id=output.po_id,
                allowed=sorted(allowed),
            )

    return check
