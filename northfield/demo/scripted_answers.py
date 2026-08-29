"""Scripted model answers, used to author the cassettes the demo replays.

**These are fixtures, not recordings.** They exist so that ``make demo`` works on a
clean checkout with no API key and no cost, and so that tests are deterministic.
They are written by hand to be representative of what the model returns.

To replace them with genuine recordings from a live model::

    export ANTHROPIC_API_KEY=...
    make record          # runs every scenario live and overwrites cassettes/

Each cassette records the prompt alongside the answer, so a reviewer can read what
the model was asked. Because the cassette key includes the prompt text, editing a
prompt orphans its cassette and the replay run fails loudly rather than quietly
answering the previous question.

The answers below are deliberately *plausible rather than perfect*. The
supplier choice picks Meridian over cheaper Halstead — the reasoning a good buyer
would give — and the free-form plan for Scenario B is the one a quality manager
would assemble. Where a fixture is wrong on purpose, the failure suite says so.
"""

from __future__ import annotations

import re
from typing import Any

from harmony.llm.client import LLMRequest, StubClient

# --- extraction ----------------------------------------------------------------


def _field(text: str, pattern: str) -> str | None:
    """Pull a value out of the prompt. Fixtures read their arguments from what the
    model was actually shown, so they stay correct as the context changes."""
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _normalise(text: str) -> str:
    """Collapse whitespace before matching.

    Email bodies arrive wrapped, so the sentence a fixture keys on is split across
    lines in the source. A real model reads through that; a fixture matching on raw
    substrings would not, and would fail for a reason that has nothing to do with
    the behaviour under test.
    """
    return re.sub(r"\s+", " ", text)


def _extract_commitment(request: LLMRequest) -> dict[str, Any]:
    """What a model returns when reading supplier correspondence."""
    prompt = _normalise(request.prompt)

    if "PO-77812" in prompt and "dock Tuesday 9/8" in prompt:
        return {
            "revises_delivery": True,
            "po_id": "PO-77812",
            "revised_arrival_date": "2026-09-08",
            "confidence": 0.95,
            "verbatim_quote": (
                "Revised ship date is Monday 9/7, which puts it on your dock Tuesday 9/8."
            ),
        }

    if "PO-77820" in prompt and "11th rather than the 5th" in prompt:
        return {
            "revises_delivery": True,
            "po_id": "PO-77820",
            "revised_arrival_date": "2026-09-11",
            "confidence": 0.92,
            "verbatim_quote": "We are now looking at the 11th rather than the 5th.",
        }

    # PO-77790's message reads like a shipping update and revises nothing. Getting
    # this wrong would raise a false alarm against a healthy order, which is the
    # precision case the seed data exists to test.
    return {
        "revises_delivery": False,
        "po_id": None,
        "revised_arrival_date": None,
        "confidence": 0.0,
        "verbatim_quote": None,
    }


# --- planning ------------------------------------------------------------------


def _plan(request: LLMRequest) -> dict[str, Any]:
    prompt = _normalise(request.prompt)

    # The Tuesday follow-up. Checked first, because its context also mentions the
    # part and production order that the shortfall branch keys on.
    #
    # Note what the response is *not*: another reroute. The replacement is one day
    # late against a start that is still three days out, and there is no second
    # qualified supplier who could beat that. Warning the line and re-checking
    # tomorrow is the proportionate answer, and an agent that could only escalate
    # would burn a manager's attention on a problem that has not happened yet.
    if "po_arrival_check" in prompt:
        return {
            "summary": (
                "The replacement order for P-4471 has not been received on its "
                "promised date. Production order 4812 still has three days of "
                "margin. I can warn the line and re-check tomorrow rather than "
                "reroute again."
            ),
            "reasoning": (
                "The scheduled arrival check found no goods receipt against the "
                "replacement purchase order on its promised date. 4812 does not "
                "start until 2026-09-07, so a day's slip is not yet a stoppage, and "
                "Meridian is the only qualified supplier who can beat that date — a "
                "second reroute would land no earlier. The useful actions are to "
                "give the supervisor early warning and to look again tomorrow, when "
                "a further slip would leave no margin."
            ),
            "action_kind": "tools",
            "workflow_name": None,
            "workflow_params": None,
            "tool_calls": [
                {
                    "tool": "production.notify_supervisor",
                    "params": {
                        "supervisor_id": "u-301",
                        "subject": "P-4471 replacement order is a day late",
                        "body": (
                            "The replacement order for P-4471 did not arrive on its "
                            "promised date. Production order 4812 still starts on "
                            "2026-09-07 and there is margin, but treat the material "
                            "as at risk until it is booked in. I am re-checking "
                            "tomorrow."
                        ),
                        "about": "4812",
                    },
                    "rationale": "Early warning while there is still time to react",
                },
                {
                    "tool": "schedule.create_followup",
                    "params": {
                        "detector": "po_arrival_check",
                        "fire_at": "2026-09-05",
                        "reason": "re-check the late replacement before margin runs out",
                        "payload": {
                            "po_id": _field(prompt, r'"po_id": "([^"]+)"') or "",
                            "production_order_id": "4812",
                            "expected_by": "2026-09-04",
                        },
                    },
                    "rationale": "Look again tomorrow, while a reroute is still possible",
                },
            ],
            "no_action_reason": None,
            "evidence": [
                {
                    "source": "erp",
                    "ref": "4812",
                    "detail": "starts 2026-09-07; three days of margin remain",
                }
            ],
            "alternatives_considered": [
                "Reroute again — no qualified supplier can deliver sooner than "
                "Meridian already promised, so it would not recover the day.",
                "Do nothing until the 6th — leaves the supervisor no time to "
                "resequence if the material slips again.",
            ],
        }

    if "P-4471" in prompt and "4812" in prompt:
        return {
            "summary": (
                "Part P-4471 will likely cause production order 4812 to miss its "
                "scheduled start. Kestrel says PO-77812 now arrives Tuesday 9/8, a day "
                "after the build begins. I can move the order to an approved alternate "
                "supplier and notify production. Want me to proceed?"
            ),
            "reasoning": (
                "P-4471 has 150 units on hand against 30/day usage — five days of cover, "
                "and production order 4812 starts in five days needing 120 units. "
                "PO-77812 would have covered it at the promised 09-04, but Kestrel's "
                "message M-001 revises arrival to 09-08, one day after the start, so "
                "nothing arrives in time and projected stock at start is zero. A reroute "
                "to a supplier who can deliver before 09-07 is the only option that keeps "
                "Line 2 running without moving the build."
            ),
            "action_kind": "workflow",
            "workflow_name": "po_reroute",
            "workflow_params": {
                "at_risk_po_id": "PO-77812",
                "part_id": "P-4471",
                "production_order_id": "4812",
                "required_on_site_by": "2026-09-07",
                "qty": 400,
                "supervisor_id": "u-301",
            },
            "tool_calls": None,
            "no_action_reason": None,
            "evidence": [
                {
                    "source": "mail",
                    "ref": "M-001",
                    "detail": "Kestrel revises PO-77812 arrival to 2026-09-08",
                },
                {
                    "source": "erp",
                    "ref": "PO-77812",
                    "detail": "400 units, open, promised 2026-09-04",
                },
                {
                    "source": "erp",
                    "ref": "4812",
                    "detail": "starts 2026-09-07, needs 120 of P-4471",
                },
            ],
            "alternatives_considered": [
                "Ask Kestrel to expedite or air-freight part of the quantity — they "
                "offered, but the cost is unknown and the date is not committed.",
                "Move production order 4812 out by two days — avoids the purchase but "
                "pushes a customer commitment, which is not purchasing's call.",
            ],
        }

    if "P-5540" in prompt and "4816" in prompt:
        return {
            "summary": (
                "Servo drives for production order 4816 will not arrive in time — "
                "PO-77820 has slipped to 2026-09-11, three days after the build starts. "
                "I can reroute to Voss Antriebe, who can deliver by the 8th."
            ),
            "reasoning": (
                "4816 starts 09-08 and needs 30 of P-5540. Only 12 are on hand against "
                "3/day usage, and Kestrel's message M-008 moves PO-77820 to 09-11. "
                "Voss is qualified for this part with a three-day lead time."
            ),
            "action_kind": "workflow",
            "workflow_name": "po_reroute",
            "workflow_params": {
                "at_risk_po_id": "PO-77820",
                "part_id": "P-5540",
                "production_order_id": "4816",
                "required_on_site_by": "2026-09-08",
                "qty": 40,
                "supervisor_id": "u-301",
            },
            "tool_calls": None,
            "no_action_reason": None,
            "evidence": [
                {"source": "mail", "ref": "M-008", "detail": "PO-77820 slips to 2026-09-11"},
                {"source": "erp", "ref": "4816", "detail": "starts 2026-09-08, needs 30"},
            ],
            "alternatives_considered": [
                "Partial shipment from Kestrel — they have not offered one and the "
                "winding line is the constraint."
            ],
        }

    if "L-2093" in prompt:
        return {
            "summary": (
                "Lot L-2093 is on quality hold and allocated to production order 4820, "
                "which starts in three days. Lot L-2101 is released, unallocated and "
                "large enough to cover it. I can reallocate and tell the supervisor."
            ),
            "reasoning": (
                "4820 needs 90 units of P-1188 and is allocated to L-2093, which Ingrid "
                "placed on hold for surface finish 3.4 Ra against a 3.2 spec. L-2101 "
                "holds 140 released, unallocated units — enough on its own, so no "
                "shortage needs raising with purchasing and the build date is unaffected."
            ),
            "action_kind": "tools",
            "workflow_name": None,
            "workflow_params": None,
            "tool_calls": [
                {
                    "tool": "quality.reallocate_lot",
                    "params": {
                        "production_order_id": "4820",
                        "from_lot_id": "L-2093",
                        "to_lot_id": "L-2101",
                        "reason": "L-2093 on quality hold for surface finish",
                    },
                    "rationale": "Move 4820 onto released stock that covers it in full",
                },
                {
                    "tool": "production.notify_supervisor",
                    "params": {
                        "supervisor_id": "u-301",
                        "subject": "Lot change for production order 4820",
                        "body": (
                            "Production order 4820 has been reallocated from lot L-2093 "
                            "to lot L-2101. L-2093 is on quality hold for surface finish "
                            "(3.4 Ra against a 3.2 Ra specification). L-2101 is released "
                            "and covers the full 90 units required. No change to the "
                            "scheduled start on 2026-09-05."
                        ),
                        "about": "4820",
                    },
                    "rationale": "The supervisor needs to know which lot the line will draw",
                },
            ],
            "no_action_reason": None,
            "evidence": [
                {
                    "source": "quality",
                    "ref": "L-2093",
                    "detail": "on hold since 2026-09-02, surface finish",
                },
                {
                    "source": "quality",
                    "ref": "L-2101",
                    "detail": "released, unallocated, 140 units",
                },
            ],
            "alternatives_considered": [
                "Raise a shortage flag with purchasing — unnecessary while L-2101 covers "
                "the requirement in full.",
                "Split the requirement across L-2088 and L-2101 — L-2088 is already "
                "committed to production order 4830.",
            ],
        }

    return {
        "summary": "No action is warranted.",
        "reasoning": "Nothing in the gathered context indicates a material risk.",
        "action_kind": "none",
        "workflow_name": None,
        "workflow_params": None,
        "tool_calls": None,
        "no_action_reason": "No material risk identified in the available context.",
        "evidence": [],
        "alternatives_considered": [],
    }


# --- bounded workflow steps ----------------------------------------------------


def _choose_supplier(request: LLMRequest) -> dict[str, Any]:
    """The constrained choice. Whatever this returns, the enum guardrail is what
    decides whether it is acceptable."""
    if "P-5540" in _normalise(request.prompt):
        return {
            "supplier_id": "S-T",
            "justification": (
                "Voss is the only qualified supplier whose lead time reaches us before "
                "the 8th. Their 91% on-time record is adequate for a build this close."
            ),
        }
    return {
        "supplier_id": "S-Z",
        "justification": (
            "Meridian delivers in two days against a five-day window and has a 94% "
            "on-time record, the best of the qualified suppliers. Halstead is £5.50 "
            "cheaper per unit but its nine-day lead time misses the production start "
            "entirely, so price is not the deciding factor here."
        ),
    }


def _draft_notification(request: LLMRequest) -> dict[str, Any]:
    """Drafted prose. The must_mention guardrail requires the production order and
    the replacement PO to appear, so this reads them out of the prompt."""
    prompt = _normalise(request.prompt)

    def field(label: str) -> str:
        match = re.search(rf"{label}: (\S+)", prompt)
        return match.group(1) if match else "?"

    production_order = field("Production order affected")
    replacement = field("Replacement purchase order")
    original = field("Original purchase order, now cancelled")
    supplier = field("New supplier")
    expected = field("Expected on site")
    part = field("Part")

    return {
        "subject": f"Supply change affecting production order {production_order}",
        "body": (
            f"Production order {production_order} is affected by a supplier delay on "
            f"{part.rstrip(',')}.\n\n"
            f"{original} has been cancelled and replaced by {replacement} with supplier "
            f"{supplier}. The material is expected on site on {expected}, ahead of the "
            f"scheduled start.\n\n"
            f"No change to the production schedule is needed. Raised automatically and "
            f"approved by purchasing."
        ),
    }


# --- assembly ------------------------------------------------------------------

SCRIPTED_ANSWERS = {
    "mail.extract_commitment": _extract_commitment,
    "planner": _plan,
    "workflow.po_reroute.choose_supplier": _choose_supplier,
    "workflow.po_reroute.draft_notification": _draft_notification,
}


def scripted_client() -> StubClient:
    """A client that answers every call site the scenarios reach."""
    return StubClient(SCRIPTED_ANSWERS)
