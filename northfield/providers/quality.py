"""The quality provider: lots, their status, and open shortage flags.

Added for Scenario B, and a useful measure of what adding a system costs: one
provider function, registered by a decorator, reachable because the quality
manager's profile lists ``quality`` among its providers. No change to the broker,
the session, the audit or the planner.
"""

from __future__ import annotations

from harmony.identity.session import Session
from harmony.providers.base import ContextRequest, ContextSlice, Redaction, provider
from northfield.systems import quality


@provider(
    "quality",
    description="Lot-tracked inventory, quality holds, and open shortage flags.",
    required_scopes={"quality:lot:read"},
)
def quality_provider(session: Session, request: ContextRequest) -> ContextSlice:
    store = session.services.store  # type: ignore[union-attr]
    slice_ = ContextSlice(system="quality", provider="northfield.quality")

    part_ids = request.subjects_of("part")
    lot_ids = request.subjects_of("lot")

    if lot_ids or part_ids:
        lots = [lot for lid in lot_ids if (lot := quality.get_lot(store, lid))]
        seed_parts = list(part_ids) + [lot["part_id"] for lot in lots]
    else:
        # A sweep starts from what is on hold, but the useful working set is every
        # lot of those *parts*: "this lot is on hold" is only half a finding, and
        # the other half — whether anything else could cover it — cannot be answered
        # from the held lots alone. Returning only the holds would force the
        # detector to make a second, unscoped trip to the connector.
        lots = quality.lots_on_hold(store)
        seed_parts = [lot["part_id"] for lot in lots]

    seen = {lot["lot_id"] for lot in lots}
    for part_id in dict.fromkeys(seed_parts):
        for sibling in quality.lots_for_part(store, part_id):
            if sibling["lot_id"] not in seen:
                lots.append(sibling)
                seen.add(sibling["lot_id"])

    slice_.collections["lots"] = lots
    slice_.collections["shortage_flags"] = quality.open_shortage_flags(store)

    # Holds carry the name of the person who placed them. That is fine within the
    # quality team and worth noting for anyone else.
    if not session.can("quality:hold:place"):
        slice_.redactions.append(
            Redaction(
                collection="lots",
                count=sum(1 for lot in lots if lot["hold_placed_by"]),
                reason="hold attribution requires quality:hold:place",
            )
        )
        for lot in lots:
            lot.pop("hold_placed_by", None)

    return slice_
