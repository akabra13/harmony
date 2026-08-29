"""Quality tools: finding covering lots, reallocating them, and escalating shortages.

Scenario B's toolset. Two things worth noticing:

**Different scopes, different person.** ``quality:lot:allocate`` is Priya's, not
Dana's, and ``purchasing:shortage:flag`` is how a quality problem becomes
purchasing's problem without quality acquiring the ability to raise purchase orders.
That boundary is the reason Scenario B needed a new user rather than a new
permission on an existing one.

**The escalation is a record, not a message.** ``purchasing.raise_shortage_flag``
writes a row that purchasing's own detectors can find, rather than sending an email
somebody has to notice. One agent creating work another agent can pick up is the
shape the platform is ultimately for.
"""

from __future__ import annotations

import datetime as _dt

from pydantic import BaseModel, Field

from harmony.identity.session import Session
from harmony.kernel.errors import ToolFailed
from harmony.tools.base import tool
from northfield.systems import quality

# --- reads ---------------------------------------------------------------------


class FindLotsInput(BaseModel):
    part_id: str
    min_qty: int = Field(default=0, ge=0, description="Smallest usable lot size.")


class LotCandidate(BaseModel):
    lot_id: str
    qty: int
    received_date: str
    status: str


class FindLotsOutput(BaseModel):
    part_id: str
    lots: list[LotCandidate] = Field(default_factory=list)
    lot_ids: list[str] = Field(default_factory=list)
    total_available: int = 0


@tool(
    "quality.find_available_lots",
    description=(
        "Released, unallocated lots of a part that are large enough to cover a "
        "requirement on their own."
    ),
    scopes={"quality:lot:read"},
    input=FindLotsInput,
    output=FindLotsOutput,
    system="quality",
)
def find_available_lots(session: Session, inp: FindLotsInput) -> FindLotsOutput:
    store = session.services.store  # type: ignore[union-attr]
    lots = quality.available_lots_for_part(store, inp.part_id, min_qty=inp.min_qty)
    return FindLotsOutput(
        part_id=inp.part_id,
        lots=[
            LotCandidate(
                lot_id=lot["lot_id"],
                qty=lot["qty"],
                received_date=lot["received_date"],
                status=lot["status"],
            )
            for lot in lots
        ],
        lot_ids=[lot["lot_id"] for lot in lots],
        total_available=sum(lot["qty"] for lot in lots),
    )


# --- writes --------------------------------------------------------------------


class ReallocateLotInput(BaseModel):
    production_order_id: str
    from_lot_id: str = Field(description="The lot to release from the order.")
    to_lot_id: str = Field(description="The lot to allocate in its place.")
    reason: str = ""


class ReallocateLotOutput(BaseModel):
    production_order_id: str
    from_lot_id: str
    to_lot_id: str
    from_lot_allocations: list[str]
    to_lot_allocations: list[str]


@tool(
    "quality.reallocate_lot",
    description=(
        "Move a production order's allocation from one lot to another, releasing "
        "the first."
    ),
    scopes={"quality:lot:allocate"},
    input=ReallocateLotInput,
    output=ReallocateLotOutput,
    writes=True,
    compensation="quality.revert_lot_allocation",
    system="quality",
)
def reallocate_lot(session: Session, inp: ReallocateLotInput) -> ReallocateLotOutput:
    store = session.services.store  # type: ignore[union-attr]
    source = quality.get_lot(store, inp.from_lot_id)
    target = quality.get_lot(store, inp.to_lot_id)
    if source is None or target is None:
        raise ToolFailed("one or both lots do not exist", from_lot=inp.from_lot_id, to_lot=inp.to_lot_id)

    # A lot on hold must never be allocated to production. Checked here as well as
    # upstream: this is the point of effect, and it is the last place to refuse.
    if target["status"] != "released":
        raise ToolFailed(
            f"lot {inp.to_lot_id} is {target['status']} and cannot be allocated",
            lot_id=inp.to_lot_id,
            status=target["status"],
        )
    if inp.production_order_id not in source["allocated_to"]:
        raise ToolFailed(
            f"production order {inp.production_order_id} is not allocated to "
            f"{inp.from_lot_id}"
        )

    from_allocations = [a for a in source["allocated_to"] if a != inp.production_order_id]
    to_allocations = sorted({*target["allocated_to"], inp.production_order_id})
    quality.set_allocation(store, inp.from_lot_id, from_allocations)
    quality.set_allocation(store, inp.to_lot_id, to_allocations)

    return ReallocateLotOutput(
        production_order_id=inp.production_order_id,
        from_lot_id=inp.from_lot_id,
        to_lot_id=inp.to_lot_id,
        from_lot_allocations=from_allocations,
        to_lot_allocations=to_allocations,
    )


class RevertAllocationInput(BaseModel):
    production_order_id: str
    from_lot_id: str
    to_lot_id: str


class RevertAllocationOutput(BaseModel):
    reverted: bool
    from_lot_allocations: list[str]
    to_lot_allocations: list[str]


@tool(
    "quality.revert_lot_allocation",
    description="Undo a reallocation, returning the order to its original lot.",
    scopes={"quality:lot:allocate"},
    input=RevertAllocationInput,
    output=RevertAllocationOutput,
    writes=True,
    system="quality",
)
def revert_lot_allocation(
    session: Session, inp: RevertAllocationInput
) -> RevertAllocationOutput:
    store = session.services.store  # type: ignore[union-attr]
    source = quality.get_lot(store, inp.from_lot_id)
    target = quality.get_lot(store, inp.to_lot_id)
    if source is None or target is None:
        raise ToolFailed("one or both lots do not exist")

    from_allocations = sorted({*source["allocated_to"], inp.production_order_id})
    to_allocations = [a for a in target["allocated_to"] if a != inp.production_order_id]
    quality.set_allocation(store, inp.from_lot_id, from_allocations)
    quality.set_allocation(store, inp.to_lot_id, to_allocations)

    return RevertAllocationOutput(
        reverted=True,
        from_lot_allocations=from_allocations,
        to_lot_allocations=to_allocations,
    )


class RaiseShortageInput(BaseModel):
    part_id: str
    production_order_id: str
    qty_short: int = Field(gt=0)
    needed_by: _dt.date
    note: str = ""


class RaiseShortageOutput(BaseModel):
    flag_id: str
    part_id: str
    prod_order_id: str
    qty_short: int
    status: str


@tool(
    "purchasing.raise_shortage_flag",
    description=(
        "Raise a material shortage with purchasing, for a part that cannot be "
        "covered from existing stock."
    ),
    scopes={"purchasing:shortage:flag"},
    input=RaiseShortageInput,
    output=RaiseShortageOutput,
    writes=True,
    compensation="purchasing.withdraw_shortage_flag",
    system="purchasing",
)
def raise_shortage_flag(session: Session, inp: RaiseShortageInput) -> RaiseShortageOutput:
    """Hand the problem to the team that can solve it.

    Note the scope: quality can *flag* a shortage but cannot raise a purchase order.
    Escalation across a permission boundary, rather than around it.
    """
    store = session.services.store  # type: ignore[union-attr]
    flag = quality.raise_shortage_flag(
        store,
        flag_id=session.derive_id("SHF", 4),
        part_id=inp.part_id,
        prod_order_id=inp.production_order_id,
        qty_short=inp.qty_short,
        needed_by=inp.needed_by,
        raised_by=session.principal.id,
        raised_on=session.clock.today(),
        note=inp.note,
    )
    return RaiseShortageOutput(**{k: flag[k] for k in RaiseShortageOutput.model_fields})


class WithdrawShortageInput(BaseModel):
    flag_id: str


class WithdrawShortageOutput(BaseModel):
    flag_id: str
    withdrawn: bool


@tool(
    "purchasing.withdraw_shortage_flag",
    description="Withdraw a shortage flag raised in error.",
    scopes={"purchasing:shortage:flag"},
    input=WithdrawShortageInput,
    output=WithdrawShortageOutput,
    writes=True,
    system="purchasing",
)
def withdraw_shortage_flag(
    session: Session, inp: WithdrawShortageInput
) -> WithdrawShortageOutput:
    store = session.services.store  # type: ignore[union-attr]
    return WithdrawShortageOutput(
        flag_id=inp.flag_id, withdrawn=quality.withdraw_shortage_flag(store, inp.flag_id)
    )
