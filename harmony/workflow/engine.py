"""The workflow interpreter.

The engine has no opinion about purchasing. It advances a cursor through a list of
steps, resolves bindings, invokes tools or asks bounded questions, records what
happened, and rolls back in reverse when something fails. Everything specific to a
reroute lives in the YAML.

Three properties are worth stating plainly, because they are what the definition
buys over a model-assembled plan:

**Order is not negotiable.** The engine walks ``definition.steps`` by index. There
is no branch the model can take, no step it can add, and no way to skip one. The
planner's influence ended when it supplied the parameters.

**Progress is durable.** Each step's result and the cursor advance in one
transaction, so a killed process resumes at exactly the right place.
:meth:`resume` is not a recovery path bolted on afterwards — it is the same loop,
started from a cursor that happens not to be zero.

**Failure rolls back.** When a step fails, completed steps are compensated in
reverse order. Steps declared irreversible are recorded as such rather than
pretended away, which is why the shipped definitions put irreversible effects last.
"""

from __future__ import annotations

from typing import Any

from harmony.audit.models import EventType
from harmony.identity.grant import ExecutionGrant
from harmony.identity.session import Session
from harmony.kernel.errors import CompensationFailed, WorkflowStepFailed
from harmony.kernel.ids import short_digest
from harmony.kernel.store import Store
from harmony.llm.client import LLMClient
from harmony.llm.structured import ask, must_choose_from, must_mention
from harmony.tools.base import ToolCall
from harmony.workflow.bindings import BindingContext, resolve
from harmony.workflow.loader import WorkflowCatalog
from harmony.workflow.models import (
    InstanceStatus,
    OnEmpty,
    Step,
    StepKind,
    WorkflowDefinition,
    WorkflowInstance,
)
from harmony.workflow.repository import InstanceRepository


class WorkflowEngine:
    """Starts, advances, resumes and compensates workflow instances."""

    def __init__(
        self,
        *,
        store: Store,
        catalog: WorkflowCatalog,
        invoker,
        llm: LLMClient,
    ) -> None:
        self._catalog = catalog
        self._invoker = invoker
        self._llm = llm
        self._repo = InstanceRepository(store)

    # --- entry points ----------------------------------------------------------

    def start(
        self,
        session: Session,
        *,
        definition: WorkflowDefinition,
        params: dict[str, Any],
        grant: ExecutionGrant,
    ) -> WorkflowInstance:
        """Begin a new instance and run it to completion or failure."""
        validated = definition.params_model().model_validate(params)
        # Derived, not random. Step ids are built from the instance id, idempotency
        # keys from step ids, and tool-minted identifiers from those keys — so a
        # random instance id would ripple all the way into the prompts of later
        # steps and make the run unrepeatable. See Orchestrator._run_id_for.
        instance_id = f"WF-{short_digest(session.run_id, definition.key, length=6)}"
        instance = self._repo.create(
            WorkflowInstance(
                instance_id=instance_id,
                run_id=session.run_id,
                definition_name=definition.name,
                definition_version=definition.version,
                params=validated.model_dump(mode="json"),
            ),
            now=session.clock.now(),
        )
        session.audit.emit(
            EventType.WORKFLOW_STARTED,
            f"entering {definition.key}: {definition.description}",
            instance_id=instance.instance_id,
            workflow=definition.name,
            version=definition.version,
            params=instance.params,
            steps=[s.id for s in definition.steps],
        )
        return self._advance(session, instance, definition, grant)

    def resume(
        self, session: Session, instance_id: str, *, grant: ExecutionGrant
    ) -> WorkflowInstance:
        """Continue an instance from wherever it stopped."""
        instance = self._repo.get(instance_id)
        if instance is None:
            raise WorkflowStepFailed(f"no workflow instance '{instance_id}'")

        definition = self._catalog.get(instance.definition_name, instance.definition_version)
        session.audit.emit(
            EventType.WORKFLOW_RESUMED,
            f"resuming {definition.key} at step {instance.cursor} "
            f"('{self._step_id_at(definition, instance.cursor)}')",
            instance_id=instance.instance_id,
            cursor=instance.cursor,
            completed_steps=list(instance.step_results),
        )
        return self._advance(session, instance, definition, grant)

    # --- the loop --------------------------------------------------------------

    def _advance(
        self,
        session: Session,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        grant: ExecutionGrant,
    ) -> WorkflowInstance:
        while instance.cursor < len(definition.steps):
            step = definition.steps[instance.cursor]
            try:
                output = self._run_step(session, instance, definition, step, grant)
            except Exception as exc:  # noqa: BLE001 - converted to a rollback below
                session.audit.emit(
                    EventType.WORKFLOW_STEP_FAILED,
                    f"step '{step.id}' failed: {exc}",
                    instance_id=instance.instance_id,
                    step_id=step.id,
                    cursor=instance.cursor,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                self._compensate(session, instance, definition, grant, reason=str(exc))
                return instance

            self._repo.commit_step(
                instance, step_id=step.id, output=output, now=session.clock.now()
            )
            session.audit.emit(
                EventType.WORKFLOW_STEP_COMPLETED,
                f"step '{step.id}' completed",
                instance_id=instance.instance_id,
                step_id=step.id,
                cursor=instance.cursor,
                output=output,
            )

        self._repo.set_status(instance, InstanceStatus.COMPLETED, now=session.clock.now())
        session.audit.emit(
            EventType.WORKFLOW_COMPLETED,
            f"{definition.key} completed all {len(definition.steps)} steps",
            instance_id=instance.instance_id,
            workflow=definition.name,
            steps_completed=list(instance.step_results),
        )
        return instance

    def _run_step(
        self,
        session: Session,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        step: Step,
        grant: ExecutionGrant,
    ) -> dict[str, Any]:
        session.audit.emit(
            EventType.WORKFLOW_STEP_STARTED,
            f"step '{step.id}': {step.description or step.tool or step.kind.value}",
            instance_id=instance.instance_id,
            step_id=step.id,
            kind=step.kind.value,
            cursor=instance.cursor,
        )
        ctx = self._binding_context(session, instance)
        if step.kind is StepKind.TOOL:
            return self._run_tool_step(session, instance, step, ctx, grant)
        return self._run_llm_step(session, instance, definition, step, ctx)

    # --- step kinds ------------------------------------------------------------

    def _run_tool_step(
        self,
        session: Session,
        instance: WorkflowInstance,
        step: Step,
        ctx: BindingContext,
        grant: ExecutionGrant,
    ) -> dict[str, Any]:
        params = resolve(step.input, ctx)
        result = self._invoker.invoke(
            session,
            ToolCall(
                tool=step.tool or "",
                params=params,
                step_id=f"{instance.instance_id}:{step.id}",
                rationale=step.description,
            ),
            grant=grant,
        )
        self._check_not_empty(step, result.output)
        return result.output

    @staticmethod
    def _check_not_empty(step: Step, output: dict[str, Any]) -> None:
        """Enforce ``on_empty: fail``.

        This is how a workflow states a precondition. "Confirm the alternate
        supplier is approved for the part" is only a confirmation if an empty answer
        stops the run; otherwise it is a lookup whose result nobody checked.
        """
        if step.on_empty is not OnEmpty.FAIL:
            return
        field = step.empty_check or (next(iter(output), None) if output else None)
        value = output.get(field) if field else None
        if not value:
            raise WorkflowStepFailed(
                f"step '{step.id}' returned nothing for '{field}', and the definition "
                "declares that this precondition must hold",
                step=step.id,
                field=field,
            )

    def _run_llm_step(
        self,
        session: Session,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        step: Step,
        ctx: BindingContext,
    ) -> dict[str, Any]:
        """Ask a bounded question.

        The bounds come from three places, none of which the model controls: the
        candidate list a previous deterministic step produced, the output schema the
        definition declares, and the ``must_mention`` guardrails. The engine builds
        the answer type here, at run time, because the permitted values are only
        known once the earlier steps have run.
        """
        enum_values: dict[str, list[str]] = {}
        for field_name, spec in step.output_schema.items():
            if spec.enum_from:
                values = resolve(f"${{{spec.enum_from}}}", ctx)
                enum_values[field_name] = [str(v) for v in _as_list(values)]

        output_model = step.output_model(f"{definition.name}_v{definition.version}", enum_values)

        guardrails = [
            must_choose_from(values, field=field) for field, values in enum_values.items()
        ]
        text_fields = [
            name
            for name, spec in step.output_schema.items()
            if spec.type == "string" and not spec.enum_from
        ]
        if step.must_mention and text_fields:
            required = [str(resolve(token, ctx)) for token in step.must_mention]
            guardrails.append(must_mention(*required, field=text_fields[-1]))

        answer = ask(
            self._llm,
            session,
            call_site=f"workflow.{definition.name}.{step.id}",
            system=step.system
            or (
                "You are a step inside a fixed business workflow. Answer only the "
                "question asked, within the constraints given. You are not deciding "
                "what happens next; the workflow decides that."
            ),
            prompt=str(resolve(step.prompt, ctx)),
            output_model=output_model,
            guardrails=guardrails,
        )
        return answer.model_dump(mode="json")

    # --- compensation ----------------------------------------------------------

    def _compensate(
        self,
        session: Session,
        instance: WorkflowInstance,
        definition: WorkflowDefinition,
        grant: ExecutionGrant,
        *,
        reason: str,
    ) -> None:
        """Undo completed steps in reverse order.

        Reverse order is not stylistic. Later steps were built on earlier ones —
        the original PO was reduced *because* the replacement exists — so undoing
        forwards would briefly leave a state that never legitimately occurred.
        """
        now = session.clock.now()
        self._repo.set_status(instance, InstanceStatus.COMPENSATING, now=now, error=reason)
        completed = [s for s in definition.steps if s.id in instance.step_results]
        session.audit.emit(
            EventType.WORKFLOW_COMPENSATING,
            f"rolling back {len(completed)} completed step(s) in reverse order",
            instance_id=instance.instance_id,
            reason=reason,
            steps=[s.id for s in reversed(completed)],
        )

        failures: list[str] = []
        for step in reversed(completed):
            entry = self._compensate_step(session, instance, step, grant)
            self._repo.record_compensation(instance, entry, now=session.clock.now())
            if entry["outcome"] == "failed":
                failures.append(step.id)

        if failures:
            self._repo.set_status(
                instance,
                InstanceStatus.COMPENSATION_FAILED,
                now=session.clock.now(),
                error=f"{reason}; compensation failed for {failures}",
            )
            session.audit.emit(
                EventType.WORKFLOW_COMPENSATION_FAILED,
                f"rollback incomplete — effects remain from {failures}. Human intervention required.",
                instance_id=instance.instance_id,
                failed_steps=failures,
            )
            raise CompensationFailed(
                f"workflow {instance.key} could not be fully rolled back",
                instance_id=instance.instance_id,
                failed_steps=failures,
            )

        self._repo.set_status(instance, InstanceStatus.COMPENSATED, now=session.clock.now())
        session.audit.emit(
            EventType.WORKFLOW_COMPENSATED,
            f"{instance.key} rolled back cleanly",
            instance_id=instance.instance_id,
            reason=reason,
        )

    def _compensate_step(
        self,
        session: Session,
        instance: WorkflowInstance,
        step: Step,
        grant: ExecutionGrant,
    ) -> dict[str, Any]:
        if step.compensation is None:
            outcome = "irreversible" if step.irreversible else "nothing_to_undo"
            session.audit.emit(
                EventType.WORKFLOW_COMPENSATION_STEP,
                f"step '{step.id}' has no compensation ({outcome})",
                instance_id=instance.instance_id,
                step_id=step.id,
                outcome=outcome,
            )
            return {"step_id": step.id, "outcome": outcome}

        ctx = self._binding_context(session, instance)
        try:
            params = resolve(step.compensation.input, ctx)
            result = self._invoker.invoke(
                session,
                ToolCall(
                    tool=step.compensation.tool,
                    params=params,
                    step_id=f"{instance.instance_id}:{step.id}:compensate",
                    rationale=f"compensating step '{step.id}'",
                ),
                grant=_widen(grant, step.compensation.tool),
            )
        except Exception as exc:  # noqa: BLE001 - recorded and reported
            session.audit.emit(
                EventType.WORKFLOW_COMPENSATION_STEP,
                f"could not undo step '{step.id}': {exc}",
                instance_id=instance.instance_id,
                step_id=step.id,
                outcome="failed",
                error=str(exc),
            )
            return {"step_id": step.id, "outcome": "failed", "error": str(exc)}

        session.audit.emit(
            EventType.WORKFLOW_COMPENSATION_STEP,
            f"undid step '{step.id}' via {step.compensation.tool}",
            instance_id=instance.instance_id,
            step_id=step.id,
            outcome="compensated",
            tool=step.compensation.tool,
            output=result.output,
        )
        return {"step_id": step.id, "outcome": "compensated", "tool": step.compensation.tool}

    # --- helpers ---------------------------------------------------------------

    def _binding_context(self, session: Session, instance: WorkflowInstance) -> BindingContext:
        return BindingContext(
            params=instance.params,
            step_results=instance.step_results,
            clock=session.clock,
        )

    @staticmethod
    def _step_id_at(definition: WorkflowDefinition, cursor: int) -> str:
        if cursor < len(definition.steps):
            return definition.steps[cursor].id
        return "<end>"

    def instances_for_run(self, run_id: str) -> list[WorkflowInstance]:
        return self._repo.for_run(run_id)

    def unfinished(self) -> list[WorkflowInstance]:
        return self._repo.unfinished()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _widen(grant: ExecutionGrant, tool: str) -> ExecutionGrant:
    """Extend a grant to cover one compensation tool.

    A grant authorises the plan's tools; rolling that plan back is part of the same
    consent. Compensation tools come from the definition, never from the model, so
    widening here cannot admit anything a human did not implicitly approve when
    they approved a workflow whose rollback path was declared.
    """
    return ExecutionGrant(
        proposal_digest=grant.proposal_digest,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        allowed_tools=grant.allowed_tools | {tool},
        approval_id=grant.approval_id,
        reason="compensation",
    )
