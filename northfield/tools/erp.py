"""ERP tools: what the agent can look up and change in the purchasing system.

Two of these are reads that exist to make the reroute workflow's first two steps
real. Purchasing said *"confirm the alternate supplier is approved for the part,
confirm their lead time meets the production date"*, and those are only confirmations
if something can fail them. So they are tools with declared outputs and an
``on_empty: fail`` guard in the definition, rather than filtering done inside a
prompt where nobody could audit it.

The write tools each declare their inverse. ``erp.create_purchase_order`` is undone
by cancelling it; ``erp.cancel_or_reduce_purchase_order`` is undone by restoring the
status and quantity it recorded on the way past. That second one is why the tool
returns its previous state: a compensation that has to guess what it is reverting to
is not a compensation.
"""

from __future__ import annotations

import datetime as _dt

from pydantic import BaseModel, Field

from harmony.identity.session import Session
from harmony.kernel.errors import ToolFailed
from harmony.tools.base import tool
from northfield.systems import erp

# --- reads ---------------------------------------------------------------------


class ApprovedSuppliersInput(BaseModel):
    part_id: str = Field(description="The part to find qualified suppliers for.")


class SupplierCandidate(BaseModel):
    supplier_id: str
    name: str
    lead_time_days: int
    unit_price: float | None = None
    on_time_rate: float


class ApprovedSuppliersOutput(BaseModel):
    part_id: str
    suppliers: list[SupplierCandidate] = Field(default_factory=list)
    supplier_ids: list[str] = Field(default_factory=list)
    """Flattened for the workflow's ``enum_from`` binding: the model's choice is
    constrained to exactly this list."""

    rejected: list[dict] = Field(default_factory=list)
    """Suppliers considered and excluded, with the reason. Audited, so "why was the
    cheap supplier not used?" is answerable without re-running anything."""


@tool(
    "erp.list_approved_suppliers_for_part",
    description=(
        "Suppliers qualified to supply a specific part. A supplier being approved "
        "as a vendor is not sufficient; the part must be on their qualification list."
    ),
    scopes={"erp:part:read"},
    input=ApprovedSuppliersInput,
    output=ApprovedSuppliersOutput,
    system="erp",
)
def list_approved_suppliers_for_part(
    session: Session, inp: ApprovedSuppliersInput
) -> ApprovedSuppliersOutput:
    store = session.services.store  # type: ignore[union-attr]
    approved = erp.suppliers_approved_for(store, inp.part_id)
    approved_ids = {s["supplier_id"] for s in approved}

    rejected = [
        {
            "supplier_id": s["supplier_id"],
            "name": s["name"],
            "reason": (
                "not an approved vendor"
                if not s["approved"]
                else f"not qualified to supply {inp.part_id}"
            ),
        }
        for s in erp.list_suppliers(store)
        if s["supplier_id"] not in approved_ids
    ]

    return ApprovedSuppliersOutput(
        part_id=inp.part_id,
        suppliers=[
            SupplierCandidate(
                supplier_id=s["supplier_id"],
                name=s["name"],
                lead_time_days=s["lead_time_days"],
                unit_price=s["pricing"].get(inp.part_id),
                on_time_rate=s["on_time_rate"],
            )
            for s in approved
        ],
        supplier_ids=sorted(approved_ids),
        rejected=rejected,
    )


class LeadTimeFilterInput(BaseModel):
    candidates: list[SupplierCandidate] = Field(
        description="Suppliers to filter, from the approved-supplier step."
    )
    need_by: _dt.date = Field(description="The date the goods must be on site.")
    as_of: _dt.date = Field(description="The date an order would be placed.")


class LeadTimeFilterOutput(BaseModel):
    suppliers: list[SupplierCandidate] = Field(default_factory=list)
    supplier_ids: list[str] = Field(default_factory=list)
    arrival_dates: dict[str, str] = Field(default_factory=dict)
    rejected: list[dict] = Field(default_factory=list)


@tool(
    "erp.filter_suppliers_by_lead_time",
    description=(
        "Narrow a list of suppliers to those whose lead time gets goods on site by "
        "a required date."
    ),
    scopes={"erp:part:read"},
    input=LeadTimeFilterInput,
    output=LeadTimeFilterOutput,
    system="erp",
)
def filter_suppliers_by_lead_time(
    session: Session, inp: LeadTimeFilterInput
) -> LeadTimeFilterOutput:
    """Arithmetic, done in code.

    Halstead Precision is cheaper than Meridian and approved for the part; their
    nine-day lead time is what disqualifies them. Doing this subtraction here rather
    than in a prompt is the difference between a constraint and a suggestion.
    """
    keep: list[SupplierCandidate] = []
    arrivals: dict[str, str] = {}
    rejected: list[dict] = []

    for candidate in inp.candidates:
        arrival = inp.as_of + _dt.timedelta(days=candidate.lead_time_days)
        if arrival <= inp.need_by:
            keep.append(candidate)
            arrivals[candidate.supplier_id] = arrival.isoformat()
        else:
            rejected.append(
                {
                    "supplier_id": candidate.supplier_id,
                    "name": candidate.name,
                    "reason": (
                        f"{candidate.lead_time_days}-day lead time arrives "
                        f"{arrival.isoformat()}, after {inp.need_by.isoformat()}"
                    ),
                }
            )

    return LeadTimeFilterOutput(
        suppliers=keep,
        supplier_ids=[c.supplier_id for c in keep],
        arrival_dates=arrivals,
        rejected=rejected,
    )


# --- writes --------------------------------------------------------------------


class CreatePOInput(BaseModel):
    part_id: str
    supplier_id: str
    qty: int = Field(gt=0)
    need_by: _dt.date
    replaces_po: str | None = None
    reason: str = ""


class CreatePOOutput(BaseModel):
    po_id: str
    part_id: str
    supplier_id: str
    qty: int
    unit_price: float
    total_value: float
    promised_date: str
    status: str


@tool(
    "erp.create_purchase_order",
    description="Raise a new purchase order with a supplier.",
    scopes={"erp:po:create"},
    input=CreatePOInput,
    output=CreatePOOutput,
    writes=True,
    compensation="erp.cancel_purchase_order",
    system="erp",
)
def create_purchase_order(session: Session, inp: CreatePOInput) -> CreatePOOutput:
    store = session.services.store  # type: ignore[union-attr]
    supplier = erp.get_supplier(store, inp.supplier_id)
    if supplier is None:
        raise ToolFailed(f"no supplier '{inp.supplier_id}'")

    # Defence in depth. The gate has already refused unqualified suppliers and the
    # workflow never offered one; this is the last line, at the point of effect.
    if not supplier["approved"] or inp.part_id not in supplier["approved_parts"]:
        raise ToolFailed(
            f"{inp.supplier_id} is not qualified to supply {inp.part_id}",
            supplier_id=inp.supplier_id,
            part_id=inp.part_id,
        )

    unit_price = supplier["pricing"].get(inp.part_id)
    if unit_price is None:
        raise ToolFailed(f"no agreed price for {inp.part_id} from {inp.supplier_id}")

    today = session.clock.today()
    promised = today + _dt.timedelta(days=supplier["lead_time_days"])
    if promised > inp.need_by:
        raise ToolFailed(
            f"{inp.supplier_id} cannot deliver by {inp.need_by.isoformat()} "
            f"(earliest {promised.isoformat()})"
        )

    po = erp.create_purchase_order(
        store,
        po_id=session.derive_id("PO"),
        part_id=inp.part_id,
        supplier_id=inp.supplier_id,
        qty=inp.qty,
        unit_price=unit_price,
        ordered_date=today,
        promised_date=promised,
        created_by=session.principal.id,
        notes=inp.reason,
        replaces_po=inp.replaces_po,
    )
    return CreatePOOutput(**{k: po[k] for k in CreatePOOutput.model_fields})


class CancelPOInput(BaseModel):
    po_id: str
    reason: str = ""


class CancelPOOutput(BaseModel):
    po_id: str
    previous_status: str
    previous_qty: int
    status: str


@tool(
    "erp.cancel_purchase_order",
    description="Cancel an open purchase order outright.",
    scopes={"erp:po:cancel"},
    input=CancelPOInput,
    output=CancelPOOutput,
    writes=True,
    compensation="erp.restore_purchase_order",
    system="erp",
)
def cancel_purchase_order(session: Session, inp: CancelPOInput) -> CancelPOOutput:
    store = session.services.store  # type: ignore[union-attr]
    po = erp.get_purchase_order(store, inp.po_id)
    if po is None:
        raise ToolFailed(f"no purchase order '{inp.po_id}'")

    erp.set_purchase_order_status(store, inp.po_id, "cancelled", note=inp.reason)
    return CancelPOOutput(
        po_id=inp.po_id,
        previous_status=po["status"],
        previous_qty=po["qty"],
        status="cancelled",
    )


class ReducePOInput(BaseModel):
    po_id: str
    covered_elsewhere: int = Field(
        ge=0,
        description=(
            "How much of this order's quantity is now being supplied by someone "
            "else. The order is reduced by this amount, and cancelled outright if "
            "nothing is left."
        ),
    )
    reason: str = ""


class ReducePOOutput(BaseModel):
    po_id: str
    previous_status: str
    previous_qty: int
    status: str
    qty: int
    action: str


@tool(
    "erp.cancel_or_reduce_purchase_order",
    description=(
        "Reduce an open purchase order by a quantity now sourced elsewhere, or "
        "cancel it outright if that leaves nothing. Returns the previous state so "
        "the change can be undone."
    ),
    scopes={"erp:po:cancel"},
    input=ReducePOInput,
    output=ReducePOOutput,
    writes=True,
    compensation="erp.restore_purchase_order",
    system="erp",
)
def cancel_or_reduce_purchase_order(session: Session, inp: ReducePOInput) -> ReducePOOutput:
    store = session.services.store  # type: ignore[union-attr]
    po = erp.get_purchase_order(store, inp.po_id)
    if po is None:
        raise ToolFailed(f"no purchase order '{inp.po_id}'")
    if po["status"] not in ("open", "partial"):
        raise ToolFailed(
            f"purchase order {inp.po_id} is {po['status']} and cannot be changed",
            po_id=inp.po_id,
            status=po["status"],
        )

    # The arithmetic lives here rather than in a workflow binding, because bindings
    # deliberately move values and never compute them. It matters: a replacement
    # that covers only the shortfall must leave the remainder on the original order,
    # or the difference is silently destroyed. Cancelling outright is the special
    # case where the replacement covers everything.
    new_qty = max(0, po["qty"] - inp.covered_elsewhere)

    if new_qty == 0:
        erp.set_purchase_order_status(store, inp.po_id, "cancelled", note=inp.reason)
        action, status, qty = "cancelled", "cancelled", 0
    else:
        erp.set_purchase_order_qty(store, inp.po_id, new_qty)
        erp.set_purchase_order_status(store, inp.po_id, po["status"], note=inp.reason)
        action, status, qty = "reduced", po["status"], new_qty

    return ReducePOOutput(
        po_id=inp.po_id,
        previous_status=po["status"],
        previous_qty=po["qty"],
        status=status,
        qty=qty,
        action=action,
    )


class RestorePOInput(BaseModel):
    po_id: str
    status: str = Field(description="The status to restore.")
    qty: int = Field(gt=0, description="The quantity to restore.")


class RestorePOOutput(BaseModel):
    po_id: str
    status: str
    qty: int
    restored: bool


@tool(
    "erp.restore_purchase_order",
    description="Restore a purchase order's previous status and quantity.",
    scopes={"erp:po:cancel"},
    input=RestorePOInput,
    output=RestorePOOutput,
    writes=True,
    system="erp",
)
def restore_purchase_order(session: Session, inp: RestorePOInput) -> RestorePOOutput:
    """Compensation for cancelling or reducing.

    Honest about its limits: this restores the record, and in a real ERP the
    supplier may already have acted on the cancellation. DESIGN.md discusses why
    compensation gets harder against real systems and what follows from that.
    """
    store = session.services.store  # type: ignore[union-attr]
    if erp.get_purchase_order(store, inp.po_id) is None:
        raise ToolFailed(f"no purchase order '{inp.po_id}' to restore")

    erp.set_purchase_order_qty(store, inp.po_id, inp.qty)
    erp.set_purchase_order_status(
        store, inp.po_id, inp.status, note="restored by compensation"
    )
    restored = erp.get_purchase_order(store, inp.po_id)
    return RestorePOOutput(
        po_id=inp.po_id,
        status=restored["status"],
        qty=restored["qty"],
        restored=restored["status"] == inp.status and restored["qty"] == inp.qty,
    )


class RecordReceiptInput(BaseModel):
    po_id: str
    qty: int = Field(gt=0)


class RecordReceiptOutput(BaseModel):
    receipt_id: str
    po_id: str
    qty: int


@tool(
    "erp.record_goods_receipt",
    description="Record that goods against a purchase order arrived.",
    scopes={"erp:po:create"},
    input=RecordReceiptInput,
    output=RecordReceiptOutput,
    writes=True,
    system="erp",
)
def record_goods_receipt(session: Session, inp: RecordReceiptInput) -> RecordReceiptOutput:
    """Used by the demo to make the "the shipment did arrive" branch of the Tuesday
    check reachable, and by goods-in in a real deployment."""
    store = session.services.store  # type: ignore[union-attr]
    po = erp.get_purchase_order(store, inp.po_id)
    if po is None:
        raise ToolFailed(f"no purchase order '{inp.po_id}'")

    receipt = erp.record_goods_receipt(
        store,
        receipt_id=session.derive_id("GR", 4),
        po_id=inp.po_id,
        part_id=po["part_id"],
        qty=inp.qty,
        received_date=session.clock.today(),
        received_by=session.principal.id,
    )
    erp.set_purchase_order_status(
        store, inp.po_id, "received" if inp.qty >= po["qty"] else "partial"
    )
    return RecordReceiptOutput(**receipt)
