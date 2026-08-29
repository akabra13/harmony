"""The free-form executor: running a plan the model assembled.

This is Scenario B's path, and the honest thing to say about it is that it offers
weaker guarantees than a declared workflow, by construction:

===================  ==================  =====================================
                     declared workflow   free-form tool plan
===================  ==================  =====================================
step order           fixed by definition chosen by the model, then frozen
resumable            yes, from a cursor  no — a killed run is re-planned
compensation input   declared bindings   inferred from the original call
preconditions        ``on_empty: fail``  none
===================  ==================  =====================================

The compensation row is the sharp one. A workflow states exactly how to undo a
step — ``cancel_purchase_order(po_id: ${steps.create.output.po_id})``. A free-form
plan has no such declaration, so the executor falls back to a convention: invoke
the tool's declared compensation with the original call's parameters merged with its
outputs, and record a failure if that does not validate. It usually works, because
compensations tend to take the identifier their forward tool produced. "Usually
works" is not a rollback guarantee.

That gap is the argument for the design change discussed in DESIGN.md: if the
free-form path also produced a graph — a small one, generated rather than declared,
but gate-checked and then executed by the same engine — it would inherit
resumption and compensation instead of approximating them. The two paths exist
here because the harness needs both today, not because the split is right.
"""

from __future__ import annotations

from typing import Any

from harmony.audit.models import EventType
from harmony.identity.grant import ExecutionGrant
from harmony.identity.session import Session
from harmony.plan.models import ToolPlan
from harmony.tools.base import ToolResult
from harmony.tools.catalog import ToolCatalog
from harmony.tools.invoker import ToolInvoker


class PlanExecutor:
    """Runs a sequence of tool calls, compensating in reverse if one fails."""

    def __init__(self, *, invoker: ToolInvoker, catalog: ToolCatalog) -> None:
        self._invoker = invoker
        self._catalog = catalog

    def execute(
        self, session: Session, plan: ToolPlan, *, grant: ExecutionGrant
    ) -> list[ToolResult]:
        """Run every call in order. Raises after compensating if one fails."""
        completed: list[ToolResult] = []
        for call in plan.calls:
            try:
                completed.append(self._invoker.invoke(session, call, grant=grant))
            except Exception as exc:  # noqa: BLE001 - rolled back, then re-raised
                session.audit.emit(
                    EventType.WORKFLOW_COMPENSATING,
                    f"plan failed at '{call.tool}'; rolling back "
                    f"{len(completed)} completed call(s)",
                    failed_tool=call.tool,
                    error=str(exc),
                    completed=[r.tool for r in completed],
                )
                self._compensate(session, completed, plan, grant)
                raise

        return completed

    def _compensate(
        self,
        session: Session,
        completed: list[ToolResult],
        plan: ToolPlan,
        grant: ExecutionGrant,
    ) -> None:
        """Undo completed calls in reverse order, best-effort.

        Every outcome is audited, including the ones that could not be undone. An
        operator reading the trail must be able to tell what state the systems were
        left in, and "we tried and could not" is information they need more than a
        clean-looking log.
        """
        params_by_step = {call.step_id: call.params for call in plan.calls}

        for result in reversed(completed):
            spec = self._catalog.get(result.tool)
            if spec.compensation is None:
                session.audit.emit(
                    EventType.WORKFLOW_COMPENSATION_STEP,
                    f"'{result.tool}' has no compensation; its effect stands",
                    tool=result.tool,
                    outcome="irreversible",
                )
                continue

            params = self._infer_compensation_params(
                original_params=params_by_step.get(result.step_id, {}),
                output=result.output,
            )
            try:
                self._invoker.compensate(
                    session, original=result, params=params, grant=grant
                )
                session.audit.emit(
                    EventType.WORKFLOW_COMPENSATION_STEP,
                    f"undid '{result.tool}' via {spec.compensation}",
                    tool=result.tool,
                    compensation=spec.compensation,
                    outcome="compensated",
                )
            except Exception as exc:  # noqa: BLE001 - recorded, loop continues
                session.audit.emit(
                    EventType.WORKFLOW_COMPENSATION_FAILED,
                    f"could not undo '{result.tool}': {exc}. Effects remain in "
                    f"{spec.system}; human intervention required.",
                    tool=result.tool,
                    compensation=spec.compensation,
                    outcome="failed",
                    error=str(exc),
                )

    @staticmethod
    def _infer_compensation_params(
        *, original_params: dict[str, Any], output: dict[str, Any]
    ) -> dict[str, Any]:
        """The convention this path rests on.

        Outputs take precedence over inputs, because a compensation almost always
        needs the identifier the forward call produced rather than the arguments it
        was given. The invoker validates the result against the compensation tool's
        schema, so a bad inference fails cleanly rather than doing something
        unintended.
        """
        return {**original_params, **output}
