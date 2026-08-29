"""Did the shipment actually arrive?

This is the Tuesday follow-up, and it is worth being clear about what it is *not*:
it is not a special second act of Scenario A, and it is not a callback the workflow
registered. It is an ordinary detector that happens to take an argument, invoked by
an ordinary scheduled task.

That uniformity is the payoff. The follow-up goes through the same dedupe, the same
context gathering, the same planner, the same gate and the same approval as a
scheduled sweep, because as far as the harness is concerned it *is* one — a
detection that happened to be aimed at a specific purchase order rather than swept
across all of them. Nothing in the loop knows the difference, which is why the
second pass needed no new code.

It answers from goods receipts, not from the promised date. A purchase order that
says it arrived on Tuesday and a receipt that says it did are different claims, and
only the second is evidence.
"""

from __future__ import annotations

from collections.abc import Iterable

from harmony.detect.base import DetectorContext, detector
from harmony.detect.models import AttentionItem, Evidence, Severity
from harmony.kernel.clock import parse_date
from harmony.providers.base import SubjectRef


@detector(
    "po_arrival_check",
    description=(
        "Confirm a specific purchase order was received by its expected date, and "
        "raise the impact on production if it was not."
    ),
    systems={"erp"},
    required_scopes={"erp:part:read", "erp:po:read", "erp:production:read", "erp:receipt:read"},
    targeted=True,
)
def detect(ctx: DetectorContext) -> Iterable[AttentionItem]:
    po_id = ctx.payload.get("po_id")
    if not po_id:
        return  # a sweep with no target has nothing to check

    expected_by = ctx.payload.get("expected_by")
    production_order_id = ctx.payload.get("production_order_id")
    today = ctx.clock.today()

    subjects = [SubjectRef(kind="purchase_order", id=po_id)]
    if production_order_id:
        subjects.append(SubjectRef(kind="production_order", id=production_order_id))

    bundle = ctx.scan(
        purpose=f"confirm {po_id} was received", subjects=subjects
    )

    purchase_order = bundle.one("erp", "purchase_orders", po_id=po_id)
    if purchase_order is None:
        return

    receipts = [r for r in bundle.records("erp", "goods_receipts") if r["po_id"] == po_id]
    received_qty = sum(r["qty"] for r in receipts)

    if received_qty >= purchase_order["qty"]:
        # Arrived. Nothing to raise — and the absence of an alert here is itself a
        # result, recorded because the detector ran and reported.
        return

    order = bundle.one("erp", "production_orders", prod_order_id=production_order_id or "")
    yield AttentionItem.build(
        detector_id="po_arrival_check",
        principal_id=ctx.session.principal.id,
        title=(
            f"{po_id} has not been received"
            + (f"; production order {production_order_id} is at risk" if order else "")
        ),
        severity=Severity.CRITICAL if order else Severity.HIGH,
        subjects=subjects + [SubjectRef(kind="part", id=purchase_order["part_id"])],
        facts={
            "purchase_order": {
                "id": po_id,
                "part_id": purchase_order["part_id"],
                "supplier_id": purchase_order["supplier_id"],
                "qty_ordered": purchase_order["qty"],
                "qty_received": received_qty,
                "promised_date": purchase_order["promised_date"],
                "status": purchase_order["status"],
            },
            "check": {
                "expected_by": expected_by,
                "checked_on": today.isoformat(),
                "days_overdue": (today - parse_date(expected_by)).days
                if expected_by
                else None,
                "receipts_found": len(receipts),
            },
            "production_order": {
                "id": order["prod_order_id"],
                "product": order["product"],
                "line": order["line"],
                "scheduled_start": order["scheduled_start"],
                "supervisor_id": order["supervisor_id"],
            }
            if order
            else None,
            "reason_scheduled": ctx.payload.get("reason", ""),
        },
        evidence=[
            Evidence(
                source="erp",
                ref=po_id,
                detail=(
                    f"ordered {purchase_order['qty']}, received {received_qty}; "
                    f"expected by {expected_by or purchase_order['promised_date']}"
                ),
            )
        ],
        now=ctx.clock.now(),
    )
