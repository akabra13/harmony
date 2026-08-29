"""Detect production orders whose material will not arrive in time.

This detector is the reason Scenario A starts without anyone asking. It is also
entirely arithmetic: every number below was computed by code, from typed data, and
the only model output it consumes is a *date* that the mail provider extracted from
correspondence.

The projection, for one component of one planned order:

    days_until_start   = start - today
    baseline_consumed  = daily_usage × days_until_start
    arriving_in_time   = Σ qty of open POs whose effective arrival ≤ start
    projected_on_hand  = on_hand − baseline_consumed + arriving_in_time

    shortfall ⟺ projected_on_hand < the quantity this order needs

"Effective arrival" is where the email matters. PO-77812 is promised for 09-04,
which is comfortably before 4812 starts on 09-07 — on the promised date there is no
problem at all. Kestrel's revised date of 09-08 is what turns a healthy supply
position into a line stoppage, and it exists only in prose. That is the whole case
for having a model in the loop, and the whole case for keeping it out of everything
else.

Note what the detector does *not* do: it does not decide that a reroute is the
answer, or which supplier, or whether to expedite instead. It reports a shortfall
with its workings and lets the planner judge.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable

from harmony.detect.base import DetectorContext, detector
from harmony.detect.models import AttentionItem, Evidence, Severity
from harmony.kernel.clock import parse_date
from harmony.providers.base import SubjectRef

HORIZON_DAYS = 14
"""How far ahead to look. A simplification standing in for multi-level MRP netting;
MODEL.md says what a real planning run would do instead."""


@detector(
    "material_shortfall",
    description=(
        "Planned production orders whose components will not be on hand by their "
        "scheduled start, accounting for supplier commitments made in correspondence."
    ),
    systems={"erp", "mail"},
    required_scopes={"erp:part:read", "erp:po:read", "erp:production:read", "mail:read"},
)
def detect(ctx: DetectorContext) -> Iterable[AttentionItem]:
    today = ctx.clock.today()
    bundle = ctx.scan(
        purpose="scan for material shortfalls against planned production",
        horizon_days=HORIZON_DAYS,
    )

    parts = {p["part_id"]: p for p in bundle.records("erp", "parts")}
    orders = bundle.records("erp", "production_orders")
    purchase_orders = bundle.records("erp", "purchase_orders")
    commitments = _commitments_by_po(bundle.records("mail", "supplier_commitments"))

    for order in orders:
        start = parse_date(order["scheduled_start"])
        days_until_start = (start - today).days
        if days_until_start < 0:
            continue

        for component in order["components"]:
            part = parts.get(component["part_id"])
            if part is None:
                continue

            # Lot-tracked parts are somebody else's question. For those, "how much
            # is available?" is not answered by an aggregate on-hand figure — a
            # quality hold can make a well-stocked part unavailable and a released
            # lot can make a thin one fine. `lot_hold_allocation_risk` answers it
            # properly against the lots themselves; projecting against the total
            # here would double-report and would sometimes be wrong in both
            # directions at once.
            if part["lot_tracked"]:
                continue

            projection = _project(
                part=part,
                required=component["qty"],
                start=start,
                days_until_start=days_until_start,
                purchase_orders=purchase_orders,
                commitments=commitments,
            )
            if projection["projected_on_hand"] >= component["qty"]:
                continue

            yield _build_item(ctx, order, part, component, projection, commitments)


# --- the projection ------------------------------------------------------------


def _project(
    *,
    part: dict,
    required: int,
    start: _dt.date,
    days_until_start: int,
    purchase_orders: list[dict],
    commitments: dict[str, dict],
) -> dict:
    """Work out what will be on hand when the order starts, and show the working."""
    open_pos = [
        po
        for po in purchase_orders
        if po["part_id"] == part["part_id"] and po["status"] == "open"
    ]

    arriving_in_time: list[dict] = []
    arriving_late: list[dict] = []
    for po in open_pos:
        commitment = commitments.get(po["po_id"])
        arrival = (
            parse_date(commitment["revised_arrival_date"])
            if commitment
            else parse_date(po["promised_date"])
        )
        record = {
            "po_id": po["po_id"],
            "supplier_id": po["supplier_id"],
            "qty": po["qty"],
            "promised_date": po["promised_date"],
            "effective_arrival": arrival.isoformat(),
            "revised_by_supplier": bool(commitment),
        }
        (arriving_in_time if arrival <= start else arriving_late).append(record)

    baseline_consumed = round(part["daily_usage"] * days_until_start)
    incoming = sum(po["qty"] for po in arriving_in_time)
    projected = part["on_hand"] - baseline_consumed + incoming

    return {
        "part_id": part["part_id"],
        "required_qty": required,
        "on_hand_today": part["on_hand"],
        "daily_usage": part["daily_usage"],
        "days_until_start": days_until_start,
        "baseline_consumption_before_start": baseline_consumed,
        "arriving_in_time": arriving_in_time,
        "arriving_after_start": arriving_late,
        "incoming_qty_in_time": incoming,
        "projected_on_hand": projected,
        "shortfall_qty": max(0, required - projected),
        "days_of_cover": round(part["on_hand"] / part["daily_usage"], 1)
        if part["daily_usage"]
        else None,
    }


# --- item construction ---------------------------------------------------------


def _build_item(
    ctx: DetectorContext,
    order: dict,
    part: dict,
    component: dict,
    projection: dict,
    commitments: dict[str, dict],
) -> AttentionItem:
    late = projection["arriving_after_start"]
    evidence = [
        Evidence(
            source="erp",
            ref=order["prod_order_id"],
            detail=(
                f"{order['product']} on {order['line']} starts "
                f"{order['scheduled_start']} and needs {component['qty']} of "
                f"{part['part_id']}"
            ),
        ),
        Evidence(
            source="erp",
            ref=part["part_id"],
            detail=(
                f"{projection['on_hand_today']} on hand, {part['daily_usage']}/day, "
                f"{projection['days_of_cover']} days of cover; projected "
                f"{projection['projected_on_hand']} at start"
            ),
        ),
    ]

    # The supplier's own words, carried through so a human can check the extraction
    # against the sentence it came from rather than taking the date on trust.
    for po in late:
        commitment = commitments.get(po["po_id"])
        evidence.append(
            Evidence(
                source="mail" if commitment else "erp",
                ref=commitment["source_message_id"] if commitment else po["po_id"],
                detail=(
                    f"{po['po_id']} ({po['qty']} units from {po['supplier_id']}) now "
                    f"expected {po['effective_arrival']}, after the "
                    f"{order['scheduled_start']} start"
                ),
                quote=commitment.get("verbatim_quote") if commitment else None,
            )
        )

    return AttentionItem.build(
        detector_id="material_shortfall",
        principal_id=ctx.session.principal.id,
        title=(
            f"{part['part_id']} shortfall will delay production order "
            f"{order['prod_order_id']}"
        ),
        severity=_severity(projection["days_until_start"]),
        subjects=[
            SubjectRef(kind="part", id=part["part_id"]),
            SubjectRef(kind="production_order", id=order["prod_order_id"]),
            *[SubjectRef(kind="purchase_order", id=po["po_id"]) for po in late],
        ],
        facts={
            "production_order": {
                "id": order["prod_order_id"],
                "product": order["product"],
                "line": order["line"],
                "scheduled_start": order["scheduled_start"],
                "supervisor_id": order["supervisor_id"],
            },
            "part": {
                "id": part["part_id"],
                "description": part["description"],
                "unit_cost": part["unit_cost"],
            },
            "projection": projection,
        },
        evidence=evidence,
        now=ctx.clock.now(),
    )


def _severity(days_until_start: int) -> Severity:
    """Sooner is worse. Nothing subtler is warranted, and anything subtler would be
    a judgment the planner should be making instead."""
    if days_until_start <= 2:
        return Severity.CRITICAL
    if days_until_start <= 7:
        return Severity.HIGH
    return Severity.MEDIUM


def _commitments_by_po(commitments: list[dict]) -> dict[str, dict]:
    """Latest commitment per purchase order.

    Later correspondence supersedes earlier: if a supplier writes twice, the second
    letter is the one that counts.
    """
    by_po: dict[str, dict] = {}
    for commitment in commitments:
        by_po[commitment["po_id"]] = commitment
    return by_po
