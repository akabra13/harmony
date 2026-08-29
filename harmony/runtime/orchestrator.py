"""The orchestrator: the agent loop, and nothing else.

This module composes the eight responsibilities and holds none of their logic. It
should read like the state diagram in ``harmony/runtime/run.py``, and if a change
makes it read like anything else, the change probably belongs in one of the
subsystems instead.

The loop pauses. :meth:`Orchestrator.run_for_item` ends at ``AWAITING_APPROVAL``
and returns; execution resumes later, in a different process if need be, when
:meth:`Orchestrator.approve` is called. Everything needed to resume — the proposal,
the approval, the run's state — is on disk, because a loop that could only complete
inside the process that started it would not survive an approver going home.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from harmony.audit.models import EventType
from harmony.detect.base import DetectorContext
from harmony.detect.dedupe import DedupeResult
from harmony.detect.models import AttentionItem, ItemState
from harmony.gate.models import GateContext, Verdict
from harmony.identity.grant import ExecutionGrant
from harmony.identity.session import Session
from harmony.kernel.errors import HarmonyError, PlanRejected
from harmony.kernel.ids import new_id, short_digest
from harmony.memory.working import WorkingMemory
from harmony.plan.models import NoAction, Proposal, ToolPlan, WorkflowInvocation
from harmony.providers.base import ContextRequest
from harmony.runtime.harness import Harness
from harmony.runtime.profile import AgentProfile
from harmony.runtime.run import AgentRun, RunState, TriggerKind
from harmony.tools.base import ToolSpec


class Orchestrator:
    """Drives runs from detection to outcome."""

    def __init__(self, harness: Harness) -> None:
        self.h = harness

    # --- detection -------------------------------------------------------------

    def detect(
        self,
        user_id: str,
        *,
        detector_ids: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> list[DedupeResult]:
        """Run this user's detectors and admit what they find.

        Detection reads as the user, downscoped to what the detectors declared they
        need. Acting as the user is what keeps the agent's field of view identical
        to the person's — an agent that could see more would eventually tell them
        about something they are not cleared to know.
        """
        profile = self.h.profiles.for_user(user_id)
        cycle_id = f"DET-{short_digest(user_id, sorted(detector_ids or profile.detectors), self.h.clock.now().isoformat(), length=6)}"
        wanted = detector_ids or profile.detectors

        specs = [self.h.detectors.get(d) for d in wanted]
        purpose_scopes = frozenset().union(*(s.required_scopes for s in specs)) if specs else None

        session = self.h.user_session(
            user_id,
            run_id=cycle_id,
            profile_scopes=profile.scope_set(),
            purpose="detection sweep (read-only)",
        )
        session = (
            session.downscope(purpose_scopes, purpose="detection sweep")
            if purpose_scopes
            else session
        )

        results: list[DedupeResult] = []
        for spec in specs:
            session.audit.emit(
                EventType.DETECTOR_RAN,
                f"running detector '{spec.id}'",
                detector=spec.id,
                systems=sorted(spec.systems),
                cycle_id=cycle_id,
                targeted=bool(payload),
            )
            ctx = DetectorContext(
                session=session,
                clock=self.h.clock,
                broker=self.h.broker,
                systems=[s for s in profile.providers if s in spec.systems] or sorted(spec.systems),
                payload=payload or {},
            )
            for item in spec.run(ctx):
                results.append(self.h.items.admit(session, item))
        return results

    def detect_and_run(
        self,
        user_id: str,
        *,
        detector_ids: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        trigger: TriggerKind = TriggerKind.SCHEDULE,
        parent_run_id: str | None = None,
    ) -> list[AgentRun]:
        """Detect, then open a run for anything that turned out to be news."""
        profile = self.h.profiles.for_user(user_id)
        return [
            self.run_for_item(
                result.item, profile, trigger=trigger, parent_run_id=parent_run_id
            )
            for result in self.detect(user_id, detector_ids=detector_ids, payload=payload)
            if result.should_run
        ]

    # --- the loop --------------------------------------------------------------

    def run_for_item(
        self,
        item: AttentionItem,
        profile: AgentProfile,
        *,
        trigger: TriggerKind = TriggerKind.SCHEDULE,
        parent_run_id: str | None = None,
    ) -> AgentRun:
        """Take one attention item from detection to a decision."""
        now = self.h.clock.now()
        run = self.h.runs.create(
            AgentRun(
                run_id=self._run_id_for(item, profile, trigger, now),
                profile_id=profile.id,
                principal_id=item.principal_id,
                attention_item_id=item.item_id,
                trigger=trigger,
                parent_run_id=parent_run_id,
            ),
            now=now,
        )
        self.h.items.attach_run(item.item_id, run.run_id)

        session = self.h.user_session(
            item.principal_id,
            run_id=run.run_id,
            profile_scopes=profile.scope_set(),
            purpose=f"respond to {item.detector_id}",
        )
        session.audit.emit(
            EventType.RUN_STARTED,
            f"opened a run for: {item.title}",
            profile=profile.id,
            principal=item.principal_id,
            attention_item_id=item.item_id,
            detector=item.detector_id,
            trigger=trigger.value,
            parent_run_id=parent_run_id,
            scopes=sorted(session.scopes),
        )

        try:
            memory = self._gather(run, session, item, profile)
            proposal = self._plan(run, session, memory, profile)
            if proposal is None:
                return run
            return self._gate_and_execute(run, session, proposal, profile)
        except HarmonyError as exc:
            return self._fail(run, session, exc)

    def _gather(
        self, run: AgentRun, session: Session, item: AttentionItem, profile: AgentProfile
    ) -> WorkingMemory:
        self.h.runs.transition(
            run,
            RunState.GATHERING_CONTEXT,
            audit=session.audit,
            now=self.h.clock.now(),
            reason="gathering context from the systems this profile reads",
        )
        bundle = self.h.broker.gather(
            session,
            ContextRequest(
                purpose=item.title,
                subjects=item.subjects,
                hints={"detector": item.detector_id, **item.facts},
            ),
            systems=profile.providers,
        )
        recalled = self.h.memory.recall(
            session,
            scope_id=item.principal_id,
            subjects=[str(s) for s in item.subjects],
        )
        return WorkingMemory(
            item=item, context=bundle, recalled=[f.for_prompt() for f in recalled]
        )

    def _plan(
        self, run: AgentRun, session: Session, memory: WorkingMemory, profile: AgentProfile
    ) -> Proposal | None:
        """Plan, persist, and stop early if the planner declined to act."""
        self.h.runs.transition(
            run, RunState.PLANNING, audit=session.audit, now=self.h.clock.now()
        )
        proposal = self.h.planner.plan(
            session, memory, tool_patterns=profile.tools, workflow_names=profile.workflows
        )
        self.h.proposals.save(proposal, now=self.h.clock.now())
        self.h.runs.set_proposal(run, proposal.proposal_id)

        if not proposal.is_actionable:
            self.h.runs.transition(
                run,
                RunState.NO_ACTION,
                audit=session.audit,
                now=self.h.clock.now(),
                reason=f"no action warranted: {proposal.action.why}",  # type: ignore[union-attr]
            )
            self.h.items.set_state(run.attention_item_id or "", ItemState.RESOLVED)
            return None
        return proposal

    def _gate_and_execute(
        self, run: AgentRun, session: Session, proposal: Proposal, profile: AgentProfile
    ) -> AgentRun:
        self.h.runs.transition(
            run, RunState.GATING, audit=session.audit, now=self.h.clock.now()
        )
        decision = self.h.gate.evaluate(self._gate_context(session, proposal))

        if decision.verdict is Verdict.DENY:
            self.h.runs.transition(
                run,
                RunState.DENIED,
                audit=session.audit,
                now=self.h.clock.now(),
                reason=f"denied by the gate: {decision.summary()}",
            )
            return run

        if decision.verdict is Verdict.REQUIRE_APPROVAL:
            self.h.approvals.request(
                session,
                proposal=proposal,
                approver_id=decision.approver_id or session.principal.id,
                reason=decision.summary(),
            )
            self.h.runs.transition(
                run,
                RunState.AWAITING_APPROVAL,
                audit=session.audit,
                now=self.h.clock.now(),
                reason=f"waiting on {decision.approver_id}",
            )
            return run

        grant = ExecutionGrant(
            proposal_digest=proposal.digest(),
            granted_by="policy",
            granted_at=self.h.clock.now(),
            allowed_tools=frozenset(t.name for t in self._tools_for(proposal)),
            reason="permitted without approval",
        )
        return self._execute(run, session, proposal, grant)

    # --- approval resumption ---------------------------------------------------

    def approve(self, approval_id: str, *, decided_by: str, note: str = "") -> AgentRun:
        """Record an approval and execute the plan it authorises."""
        return self._decide(approval_id, approve=True, decided_by=decided_by, note=note)

    def reject(self, approval_id: str, *, decided_by: str, note: str = "") -> AgentRun:
        """Record a rejection. Nothing is executed."""
        return self._decide(approval_id, approve=False, decided_by=decided_by, note=note)

    def _decide(
        self, approval_id: str, *, approve: bool, decided_by: str, note: str
    ) -> AgentRun:
        approval = self.h.approvals.get(approval_id)
        if approval is None:
            raise HarmonyError(f"no approval request '{approval_id}'")

        run = self.h.runs.get(approval.run_id)
        proposal = self.h.proposals.get(approval.proposal_id)
        if run is None or proposal is None:
            raise HarmonyError(f"approval {approval_id} refers to a run that no longer exists")

        profile = self.h.profiles.get(run.profile_id)
        session = self.h.user_session(
            run.principal_id,
            run_id=run.run_id,
            profile_scopes=profile.scope_set(),
            purpose="execute an approved plan",
        )
        self.h.approvals.decide(
            session, approval_id=approval_id, approve=approve, decided_by=decided_by, note=note
        )

        if not approve:
            return self.h.runs.transition(
                run,
                RunState.REJECTED,
                audit=session.audit,
                now=self.h.clock.now(),
                reason=f"{decided_by} rejected the proposal"
                + (f": {note}" if note else ""),
            )

        approval = self.h.approvals.get(approval_id)  # reload with the decision on it
        grant = self.h.approvals.grant_for(
            approval,  # type: ignore[arg-type]
            proposal=proposal,
            allowed_tools=frozenset(t.name for t in self._tools_for(proposal)),
        )
        self.h.runs.transition(
            run,
            RunState.APPROVED,
            audit=session.audit,
            now=self.h.clock.now(),
            reason=f"{decided_by} approved; execution authorised for "
            f"{sorted(grant.allowed_tools)}",
        )
        return self._execute(run, session, proposal, grant)

    # --- execution -------------------------------------------------------------

    def _execute(
        self, run: AgentRun, session: Session, proposal: Proposal, grant: ExecutionGrant
    ) -> AgentRun:
        """Run the approved action down whichever path its shape implies."""
        self.h.runs.transition(
            run,
            RunState.EXECUTING,
            audit=session.audit,
            now=self.h.clock.now(),
            reason=f"executing {proposal.action.describe()}",
        )
        try:
            if isinstance(proposal.action, WorkflowInvocation):
                definition = self.h.workflows.get(
                    proposal.action.workflow, proposal.action.version
                )
                instance = self.h.engine.start(
                    session, definition=definition, params=proposal.action.params, grant=grant
                )
                state = (
                    RunState.COMPLETED
                    if instance.status.value == "completed"
                    else RunState.COMPENSATED
                )
                return self.h.runs.transition(
                    run,
                    state,
                    audit=session.audit,
                    now=self.h.clock.now(),
                    reason=f"workflow {definition.key} finished as {instance.status.value}",
                    error=instance.error,
                )

            executor = self._plan_executor()
            executor.execute(session, proposal.action, grant=grant)  # type: ignore[arg-type]
            return self.h.runs.transition(
                run,
                RunState.COMPLETED,
                audit=session.audit,
                now=self.h.clock.now(),
                reason="all planned tool calls completed",
            )
        except HarmonyError as exc:
            return self._fail(run, session, exc)

    def _fail(self, run: AgentRun, session: Session, exc: Exception) -> AgentRun:
        payload = exc.to_payload() if isinstance(exc, HarmonyError) else {"message": str(exc)}
        session.audit.emit(
            EventType.RUN_FAILED, f"run failed: {exc}", **payload
        )
        return self.h.runs.transition(
            run,
            RunState.FAILED,
            audit=session.audit,
            now=self.h.clock.now(),
            reason=str(exc),
            error=str(exc),
        )

    # --- helpers ---------------------------------------------------------------

    def _run_id_for(
        self,
        item: AttentionItem,
        profile: AgentProfile,
        trigger: TriggerKind,
        now: _dt.datetime,
    ) -> str:
        """A run identifier derived from what the run is about.

        Deterministic rather than random, and the reason reaches further than
        tidiness. Idempotency keys are derived from the run id, identifiers minted by
        tools are derived from those keys, and prompts downstream of a write embed
        the identifiers it produced. A random run id therefore makes every prompt in
        the run unrepeatable — and with it every recorded model exchange, every
        cassette, and any hope of reproducing a run to investigate it.

        Anchoring the id to the situation instead means the same starting state and
        the same clock produce the same run, byte for byte.
        """
        base = short_digest(
            profile.id,
            item.principal_id,
            item.fingerprint,
            item.content_hash,
            trigger.value,
            now.isoformat(),
            length=6,
        )
        # A superseded item has a different content hash, so a genuine repeat is
        # already distinct. This guards the remaining case: the same situation
        # resolved and raised again within the same simulated instant.
        candidate, attempt = f"RUN-{base}", 1
        while self.h.runs.get(candidate) is not None:
            attempt += 1
            candidate = f"RUN-{base}-{attempt}"
        return candidate

    def _gate_context(self, session: Session, proposal: Proposal) -> GateContext:
        definition = (
            self.h.workflows.get(proposal.action.workflow, proposal.action.version)
            if isinstance(proposal.action, WorkflowInvocation)
            else None
        )
        return GateContext(
            session=session,
            proposal=proposal,
            tools=self._tools_for(proposal),
            definition=definition,
            directory=self.h.directory,
            policy=self.h.policy,
        )

    def _tools_for(self, proposal: Proposal) -> list[ToolSpec]:
        """Every tool the proposal will invoke.

        For a workflow these come from the definition, not from the model — which is
        what makes the execution grant an accurate description of what approving
        actually authorises.
        """
        if isinstance(proposal.action, WorkflowInvocation):
            definition = self.h.workflows.get(
                proposal.action.workflow, proposal.action.version
            )
            return [self.h.tools.get(name) for name in sorted(definition.tool_names())]
        if isinstance(proposal.action, ToolPlan):
            unknown = [c.tool for c in proposal.action.calls if not self.h.tools.has(c.tool)]
            if unknown:
                raise PlanRejected(f"plan names tools that do not exist: {unknown}")
            return [self.h.tools.get(c.tool) for c in proposal.action.calls]
        if isinstance(proposal.action, NoAction):
            return []
        raise PlanRejected(f"unrecognised action type {type(proposal.action).__name__}")

    def _plan_executor(self):
        from harmony.execute.executor import PlanExecutor

        return PlanExecutor(invoker=self.h.invoker, catalog=self.h.tools)
