"""The ERP provider.

Returns the working set a run needs: the parts, suppliers, purchase orders,
production orders and receipts relevant to whatever the run is about. With named
subjects it fetches those; without them it returns the horizon — which is what a
detector sweep asks for.

Scopes are checked per collection, not once for the system. A user with
``erp:po:read`` but not ``erp:production:read`` gets purchase orders and a recorded
redaction for production orders, rather than an all-or-nothing refusal. Partial
visibility is the normal case in a real ERP, and modelling it here is what makes
the redaction machinery earn its place.
"""

from __future__ import annotations

from harmony.identity.session import Session
from harmony.providers.base import ContextRequest, ContextSlice, Redaction, provider
from northfield.systems import erp

DEFAULT_HORIZON_DAYS = 14


@provider(
    "erp",
    description="Parts, suppliers, purchase orders, production orders and goods receipts.",
    required_scopes={"erp:part:read"},
)
def erp_provider(session: Session, request: ContextRequest) -> ContextSlice:
    store = session.services.store  # type: ignore[union-attr]
    slice_ = ContextSlice(system="erp", provider="northfield.erp")
    horizon = int(request.hints.get("horizon_days", DEFAULT_HORIZON_DAYS))

    part_ids = request.subjects_of("part")
    po_ids = request.subjects_of("purchase_order")
    prod_ids = request.subjects_of("production_order")

    # --- parts ---------------------------------------------------------------
    parts = (
        [p for p in (erp.get_part(store, pid) for pid in part_ids) if p]
        if part_ids
        else erp.list_parts(store)
    )
    slice_.collections["parts"] = parts

    # --- suppliers -----------------------------------------------------------
    slice_.collections["suppliers"] = erp.list_suppliers(store)

    # --- purchase orders -----------------------------------------------------
    if session.can("erp:po:read"):
        if po_ids:
            pos = [p for p in (erp.get_purchase_order(store, p) for p in po_ids) if p]
        elif part_ids:
            pos = [po for pid in part_ids for po in erp.list_purchase_orders(store, part_id=pid)]
        else:
            pos = erp.open_purchase_orders(store)
        slice_.collections["purchase_orders"] = pos
    else:
        slice_.redactions.append(
            Redaction(
                collection="purchase_orders",
                count=len(erp.open_purchase_orders(store)),
                reason="requires erp:po:read",
            )
        )

    # --- production orders ---------------------------------------------------
    if session.can("erp:production:read"):
        if prod_ids:
            orders = [
                o for o in (erp.get_production_order(store, p) for p in prod_ids) if o
            ]
        else:
            orders = erp.production_orders_starting_within(
                store, today=session.clock.today(), horizon_days=horizon
            )
        slice_.collections["production_orders"] = orders
    else:
        slice_.redactions.append(
            Redaction(
                collection="production_orders",
                count=len(
                    erp.production_orders_starting_within(
                        store, today=session.clock.today(), horizon_days=horizon
                    )
                ),
                reason="requires erp:production:read",
            )
        )

    # --- goods receipts ------------------------------------------------------
    if session.can("erp:receipt:read"):
        receipts = [
            receipt
            for po in slice_.collections.get("purchase_orders", [])
            for receipt in erp.receipts_for_po(store, po["po_id"])
        ]
        slice_.collections["goods_receipts"] = receipts
        slice_.notes.append(
            f"{len(receipts)} receipt(s) against "
            f"{len(slice_.collections.get('purchase_orders', []))} purchase order(s)"
        )

    return slice_
