"""Northfield's policy, as gate rules.

The kernel ships two rules that are true for everyone — hold the scopes, and get a
human to agree before writing. Everything here is a decision *this company* made,
and it lives in this package for the same reason its purchase orders do.

Two rules, and the first one has an interesting problem in it.

``po_value_threshold``
    Dana may commit £25,000 without her director. But the gate runs *before* the
    workflow does, and the reroute workflow does not choose a supplier until its
    third step — so at the moment of the decision, the exact value is unknowable.

    The answer is to bound it rather than guess: cost the order at the **most
    expensive qualified supplier** for that part. If even the worst case is within
    the limit, no escalation is possible whatever the workflow chooses. If the worst
    case exceeds it, escalate — which may ask a director to approve something that
    turns out cheaper, and that is the right direction to be wrong in. A threshold
    rule that under-estimates authorises spending nobody agreed to.

``approved_supplier``
    A plan naming a supplier not qualified for the part is denied outright. This
    guards the free-form path, where the model chooses the supplier itself. Inside
    the reroute workflow the same protection comes from the first step never
    surfacing an unqualified supplier — two independent mechanisms, and the failure
    suite exercises both, because Apex Rapid Supply is cheap and fast and in Dana's
    inbox and something has to say no.
"""

from __future__ import annotations

from harmony.gate.models import GateContext, RuleVerdict
from harmony.gate.rules import gate_rule
from harmony.plan.models import ToolPlan, WorkflowInvocation
from northfield.systems import erp

PO_LIMIT_KEY = "po_create_max_value"
PO_CREATE_TOOL = "erp.create_purchase_order"


@gate_rule("po_value_threshold")
def po_value_threshold(ctx: GateContext) -> RuleVerdict:
    """Escalate to the manager when a purchase order could exceed the buyer's limit."""
    if not any(t.name == PO_CREATE_TOOL for t in ctx.tools):
        return RuleVerdict.allow("po_value_threshold", "plan raises no purchase order")

    limit = ctx.session.principal.approval_limits.limit_for(PO_LIMIT_KEY)
    if limit is None:
        return RuleVerdict.allow(
            "po_value_threshold",
            f"{ctx.session.principal.label} has no purchase-order value limit set",
        )

    estimate = _worst_case_value(ctx)
    if estimate is None:
        return RuleVerdict.allow(
            "po_value_threshold",
            "plan does not state a quantity, so no value bound could be computed",
        )

    if estimate["value"] <= limit:
        return RuleVerdict.allow(
            "po_value_threshold",
            f"worst case £{estimate['value']:,.2f} is within "
            f"{ctx.session.principal.label}'s £{limit:,.2f} limit",
            **estimate,
            limit=limit,
        )

    manager_id = ctx.session.principal.manager_id
    if not manager_id:
        return RuleVerdict.deny(
            "po_value_threshold",
            f"worst case £{estimate['value']:,.2f} exceeds the £{limit:,.2f} limit and "
            f"{ctx.session.principal.label} has no manager to escalate to",
            **estimate,
            limit=limit,
        )

    manager = ctx.directory.try_get(manager_id)
    return RuleVerdict.approval(
        "po_value_threshold",
        f"worst case £{estimate['value']:,.2f} exceeds {ctx.session.principal.label}'s "
        f"£{limit:,.2f} limit; {manager.label if manager else manager_id} must approve",
        approver_id=manager_id,
        **estimate,
        limit=limit,
    )


def _worst_case_value(ctx: GateContext) -> dict | None:
    """Bound the order's value from above, using the priciest qualified supplier.

    Works for both action shapes. A workflow supplies ``part_id`` and ``qty`` as
    parameters; a free-form plan supplies them as tool arguments. Neither has chosen
    a supplier yet — or, on the free-form path, may have — so the bound is taken over
    everyone qualified.
    """
    part_id, qty, named_supplier = _order_shape(ctx)
    if not part_id or not qty:
        return None

    store = ctx.session.services.store  # type: ignore[union-attr]
    candidates = erp.suppliers_approved_for(store, part_id)
    if named_supplier:
        candidates = [s for s in candidates if s["supplier_id"] == named_supplier] or candidates

    prices = {
        s["supplier_id"]: s["pricing"][part_id]
        for s in candidates
        if part_id in s["pricing"]
    }
    if not prices:
        return None

    worst_supplier = max(prices, key=lambda sid: prices[sid])
    return {
        "part_id": part_id,
        "qty": qty,
        "value": round(qty * prices[worst_supplier], 2),
        "priced_at": worst_supplier,
        "unit_price": prices[worst_supplier],
        "basis": "most expensive qualified supplier (upper bound)",
    }


def _order_shape(ctx: GateContext) -> tuple[str | None, int | None, str | None]:
    """Pull part, quantity and any named supplier out of either action shape."""
    action = ctx.proposal.action
    if isinstance(action, WorkflowInvocation):
        params = action.params
        return params.get("part_id"), params.get("qty"), params.get("supplier_id")
    if isinstance(action, ToolPlan):
        for call in action.calls:
            if call.tool == PO_CREATE_TOOL:
                return (
                    call.params.get("part_id"),
                    call.params.get("qty"),
                    call.params.get("supplier_id"),
                )
    return None, None, None


@gate_rule("approved_supplier")
def approved_supplier(ctx: GateContext) -> RuleVerdict:
    """Deny any plan that would order a part from a supplier not qualified for it."""
    if not isinstance(ctx.proposal.action, ToolPlan):
        return RuleVerdict.allow(
            "approved_supplier",
            "supplier is chosen inside a declared workflow, from a pre-qualified list",
        )

    store = ctx.session.services.store  # type: ignore[union-attr]
    violations: list[dict] = []

    for call in ctx.proposal.action.calls:
        if call.tool != PO_CREATE_TOOL:
            continue
        supplier_id = call.params.get("supplier_id")
        part_id = call.params.get("part_id")
        if not supplier_id or not part_id:
            continue

        supplier = erp.get_supplier(store, supplier_id)
        if supplier is None:
            violations.append({"supplier_id": supplier_id, "reason": "no such supplier"})
        elif not supplier["approved"]:
            violations.append({"supplier_id": supplier_id, "reason": "not an approved vendor"})
        elif part_id not in supplier["approved_parts"]:
            violations.append(
                {
                    "supplier_id": supplier_id,
                    "part_id": part_id,
                    "reason": f"{supplier['name']} is not qualified to supply {part_id}",
                    "qualified_for": supplier["approved_parts"],
                }
            )

    if violations:
        return RuleVerdict.deny(
            "approved_supplier",
            "; ".join(v["reason"] for v in violations),
            violations=violations,
        )
    return RuleVerdict.allow("approved_supplier", "every named supplier is qualified")
