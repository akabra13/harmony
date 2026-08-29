"""Detect production orders allocated to a lot that has been put on quality hold.

Scenario B. Structurally this is the same shape as the shortfall detector — look
ahead a horizon, compare committed supply against demand, report the gap with its
workings — but it asks the question of lots rather than of an aggregate on-hand
figure, which is what lot tracking is for.

It reports whether a covering lot exists but does not decide what to do about it.
Reallocating a good lot and flagging a shortage to purchasing are different
responses with different consequences, and choosing between them is the planner's
job. The detector's contribution is the list of candidates and their quantities.
"""

from __future__ import annotations

from collections.abc import Iterable

from harmony.detect.base import DetectorContext, detector
from harmony.detect.models import AttentionItem, Evidence, Severity
from harmony.kernel.clock import parse_date
from harmony.providers.base import SubjectRef

HORIZON_DAYS = 10


@detector(
    "lot_hold_allocation_risk",
    description=(
        "Production orders starting soon that are allocated to a lot currently on "
        "quality hold, with any alternative lots that could cover them."
    ),
    systems={"quality", "erp"},
    required_scopes={"quality:lot:read", "erp:part:read", "erp:production:read"},
)
def detect(ctx: DetectorContext) -> Iterable[AttentionItem]:
    today = ctx.clock.today()
    bundle = ctx.scan(
        purpose="scan for production allocated to lots on quality hold",
        horizon_days=HORIZON_DAYS,
    )

    lots = bundle.records("quality", "lots")
    orders = {o["prod_order_id"]: o for o in bundle.records("erp", "production_orders")}
    parts = {p["part_id"]: p for p in bundle.records("erp", "parts")}

    held = [lot for lot in lots if lot["status"] == "hold" and lot["allocated_to"]]

    for lot in held:
        for order_id in lot["allocated_to"]:
            order = orders.get(order_id)
            if order is None:
                continue

            start = parse_date(order["scheduled_start"])
            days_until_start = (start - today).days
            if days_until_start < 0 or days_until_start > HORIZON_DAYS:
                continue

            required = next(
                (c["qty"] for c in order["components"] if c["part_id"] == lot["part_id"]),
                0,
            )
            if not required:
                continue

            alternatives = _covering_lots(lots, lot, required)
            yield _build_item(
                ctx, lot, order, parts.get(lot["part_id"], {}), required,
                days_until_start, alternatives,
            )


def _covering_lots(lots: list[dict], held: dict, required: int) -> list[dict]:
    """Released, unallocated lots of the same part that are large enough alone.

    Three filters, and the seed data has a distractor for each: a scrapped lot of
    the right part, a released lot already committed elsewhere, and an available
    lot of a different part. Splitting a requirement across lots is a real
    possibility this deliberately does not model — see MODEL.md.
    """
    return [
        {
            "lot_id": lot["lot_id"],
            "qty": lot["qty"],
            "received_date": lot["received_date"],
            "covers_requirement": True,
        }
        for lot in lots
        if lot["part_id"] == held["part_id"]
        and lot["lot_id"] != held["lot_id"]
        and lot["status"] == "released"
        and not lot["allocated_to"]
        and lot["qty"] >= required
    ]


def _build_item(
    ctx: DetectorContext,
    lot: dict,
    order: dict,
    part: dict,
    required: int,
    days_until_start: int,
    alternatives: list[dict],
) -> AttentionItem:
    return AttentionItem.build(
        detector_id="lot_hold_allocation_risk",
        principal_id=ctx.session.principal.id,
        title=(
            f"Lot {lot['lot_id']} is on hold and allocated to production order "
            f"{order['prod_order_id']}, which starts in {days_until_start} day(s)"
        ),
        severity=Severity.CRITICAL if days_until_start <= 3 else Severity.HIGH,
        subjects=[
            SubjectRef(kind="lot", id=lot["lot_id"]),
            SubjectRef(kind="part", id=lot["part_id"]),
            SubjectRef(kind="production_order", id=order["prod_order_id"]),
        ],
        facts={
            "held_lot": {
                "lot_id": lot["lot_id"],
                "part_id": lot["part_id"],
                "qty": lot["qty"],
                "hold_reason": lot["hold_reason"],
                "hold_placed_on": lot["hold_placed_on"],
            },
            "production_order": {
                "id": order["prod_order_id"],
                "product": order["product"],
                "line": order["line"],
                "scheduled_start": order["scheduled_start"],
                "supervisor_id": order["supervisor_id"],
                "qty_required": required,
                "days_until_start": days_until_start,
            },
            "part": {
                "id": lot["part_id"],
                "description": part.get("description", ""),
            },
            "alternative_lots": alternatives,
            "coverage_available": bool(alternatives),
            "shortfall_qty": 0 if alternatives else required,
        },
        evidence=[
            Evidence(
                source="quality",
                ref=lot["lot_id"],
                detail=f"on hold since {lot['hold_placed_on']}: {lot['hold_reason']}",
            ),
            Evidence(
                source="erp",
                ref=order["prod_order_id"],
                detail=(
                    f"{order['product']} on {order['line']} starts "
                    f"{order['scheduled_start']} and needs {required} of {lot['part_id']}"
                ),
            ),
            *[
                Evidence(
                    source="quality",
                    ref=alt["lot_id"],
                    detail=f"released and unallocated, {alt['qty']} units — covers the {required} needed",
                )
                for alt in alternatives
            ],
        ],
        now=ctx.clock.now(),
    )
