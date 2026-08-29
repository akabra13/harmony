"""The tool invoker: the only path to a side effect.

Everything the agent does to a system of record goes through :meth:`ToolInvoker.invoke`.
There is no second route, and ``tests/architecture/test_write_chokepoint.py`` asserts
it by checking that no module outside ``northfield/tools/`` imports a system-of-record
connector.

One function, in a fixed order, applied to every call:

1. **Resolve** the tool. An unregistered name fails here, before anything else.
2. **Validate** parameters against the declared pydantic model.
3. **Authorise** — scopes, via the session, which audits the denial.
4. **Check the grant** — writes require a human-approved execution grant naming
   this tool.
5. **Replay** if this exact write already happened.
6. **Execute** inside a transaction.
7. **Record** the result under the idempotency key.
8. **Audit** the invocation and its outcome.

The ordering is deliberate: authorisation precedes execution, and the idempotency
check sits between them so that a replay is still subject to the scope check. A
principal who lost a scope cannot replay their way past it.
"""

from __future__ import annotations

from pydantic import ValidationError

from harmony.audit.models import EventType
from harmony.identity.grant import ExecutionGrant
from harmony.identity.session import Session
from harmony.kernel.errors import ApprovalRequired, PlanRejected, ToolFailed
from harmony.kernel.store import Store
from harmony.tools.base import ToolCall, ToolResult, ToolSpec
from harmony.tools.catalog import ToolCatalog
from harmony.tools.idempotency import IdempotencyStore, idempotency_key


class ToolInvoker:
    """Runs tools. Owns authorisation, idempotency and effect auditing."""

    def __init__(self, store: Store, catalog: ToolCatalog) -> None:
        self._store = store
        self._catalog = catalog
        self._idempotency = IdempotencyStore(store)

    def invoke(
        self,
        session: Session,
        call: ToolCall,
        *,
        grant: ExecutionGrant | None = None,
    ) -> ToolResult:
        """Run one tool call. Raises on failure; the caller decides about compensation."""
        spec = self._resolve(call)
        params = self._validate(spec, call)

        session.require(*spec.scopes, subject=f"invoke {spec.name}")

        if spec.writes:
            self._require_grant(session, spec, grant)

        key = idempotency_key(
            run_id=session.run_id, step_id=call.step_id, tool=spec.name, params=call.params
        )

        if spec.writes:
            replayed = self._idempotency.lookup(key)
            if replayed is not None:
                session.audit.emit(
                    EventType.TOOL_REPLAYED,
                    f"{spec.name} already performed; returning the recorded result",
                    tool=spec.name,
                    step_id=call.step_id,
                    idempotency_key=key[:16],
                )
                return ToolResult(
                    tool=spec.name,
                    step_id=call.step_id,
                    ok=True,
                    output=replayed,
                    idempotency_key=key,
                    replayed=True,
                )

        session.audit.emit(
            EventType.TOOL_INVOKED,
            call.rationale or f"invoking {spec.name}",
            tool=spec.name,
            system=spec.system,
            step_id=call.step_id,
            writes=spec.writes,
            params=call.params,
            idempotency_key=key[:16],
            grant=grant.describe() if grant and spec.writes else None,
        )

        try:
            with self._store.tx():
                # Bind the key so the tool can mint deterministic identifiers from
                # it; see Session.derive_id.
                output = spec.fn(session.for_call(key), params)
                output_dict = output.model_dump(mode="json")
                if spec.writes:
                    self._idempotency.record(
                        key=key,
                        tool=spec.name,
                        run_id=session.run_id,
                        result=output_dict,
                        now=session.clock.now(),
                    )
        except Exception as exc:  # noqa: BLE001 - re-raised as ToolFailed below
            session.audit.emit(
                EventType.TOOL_FAILED,
                f"{spec.name} failed: {exc}",
                tool=spec.name,
                step_id=call.step_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ToolFailed(
                f"tool '{spec.name}' failed: {exc}", tool=spec.name, step_id=call.step_id
            ) from exc

        session.audit.emit(
            EventType.TOOL_SUCCEEDED,
            f"{spec.name} completed",
            tool=spec.name,
            system=spec.system,
            step_id=call.step_id,
            output=output_dict,
        )
        return ToolResult(
            tool=spec.name,
            step_id=call.step_id,
            ok=True,
            output=output_dict,
            idempotency_key=key,
        )

    # --- steps of the fixed order ----------------------------------------------

    def _resolve(self, call: ToolCall) -> ToolSpec:
        if not self._catalog.has(call.tool):
            raise PlanRejected(
                f"no tool named '{call.tool}' is registered",
                tool=call.tool,
                available=self._catalog.names(),
            )
        return self._catalog.get(call.tool)

    @staticmethod
    def _validate(spec: ToolSpec, call: ToolCall):
        try:
            return spec.input_model.model_validate(call.params)
        except ValidationError as exc:
            raise PlanRejected(
                f"parameters for '{spec.name}' are invalid",
                tool=spec.name,
                errors=exc.errors(include_url=False),
            ) from exc

    @staticmethod
    def _require_grant(session: Session, spec: ToolSpec, grant: ExecutionGrant | None) -> None:
        """A write needs a human's consent to this specific plan.

        Scopes are not enough. A purchasing manager holds ``erp:po:create`` all day;
        that is what makes the job possible, not what makes any individual order
        agreed to.
        """
        if grant is None:
            session.audit.emit(
                EventType.SCOPE_DENIED,
                f"refused to run {spec.name}: no approved execution grant",
                tool=spec.name,
                reason="missing_grant",
            )
            raise ApprovalRequired(
                f"'{spec.name}' writes and requires an approved execution grant",
                tool=spec.name,
            )
        if not grant.permits(spec.name):
            session.audit.emit(
                EventType.SCOPE_DENIED,
                f"refused to run {spec.name}: outside the approved plan",
                tool=spec.name,
                reason="tool_not_in_grant",
                allowed_tools=sorted(grant.allowed_tools),
            )
            raise ApprovalRequired(
                f"'{spec.name}' is not covered by the approval for this run",
                tool=spec.name,
                allowed=sorted(grant.allowed_tools),
            )

    # --- compensation ----------------------------------------------------------

    def compensate(
        self,
        session: Session,
        *,
        original: ToolResult,
        params: dict,
        grant: ExecutionGrant | None,
    ) -> ToolResult | None:
        """Run the declared inverse of a completed call.

        Returns ``None`` when the tool declares no compensation — an irreversible
        effect, which the caller records rather than treats as an error. A
        notification that has been sent cannot be unsent, and pretending otherwise
        would make the audit trail lie.
        """
        spec = self._catalog.get(original.tool)
        if spec.compensation is None:
            return None
        comp_call = ToolCall(
            tool=spec.compensation,
            params=params,
            step_id=f"{original.step_id}:compensate",
            rationale=f"compensating {original.tool}",
        )
        comp_grant = (
            ExecutionGrant(
                proposal_digest=grant.proposal_digest,
                granted_by=grant.granted_by,
                granted_at=grant.granted_at,
                allowed_tools=grant.allowed_tools | {spec.compensation},
                approval_id=grant.approval_id,
                reason="compensation",
            )
            if grant
            else None
        )
        return self.invoke(session, comp_call, grant=comp_grant)
