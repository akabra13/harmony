"""The planner: attention item plus context, in; a typed proposal, out.

This is the one place the model is asked to exercise judgment about what should
happen, and the constraints on it are worth being explicit about:

* It sees only what the context broker returned for this session — including an
  explicit list of what it was *not* permitted to see, so it can say "I could not
  check the calendar" rather than assuming an empty calendar.
* It is offered only the tools this principal holds the scopes for and the profile
  allows, and only the workflows the profile binds. It cannot propose what it was
  never shown, and if it invents a name anyway, :meth:`_to_proposal` rejects it
  before the gate is troubled.
* Its answer is a :class:`PlannerOutput` and nothing else — no prose, no tool calls
  of its own devising, no side effects. Proposing is all it can do.

What it decides is genuinely a judgment call: whether this shortfall warrants a
reroute at all, which of several responses fits, what parameters the situation
implies. That is the part worth a model. The arithmetic that found the shortfall,
the permissions that constrain the response, and the steps a reroute consists of
are all code.
"""

from __future__ import annotations

import json
from typing import Any

from harmony.audit.models import EventType
from harmony.identity.session import Session
from harmony.kernel.errors import PlanRejected
from harmony.llm.client import LLMClient
from harmony.llm.structured import ask
from harmony.memory.working import WorkingMemory
from harmony.plan.models import (
    ActionKind,
    NoAction,
    PlannedCall,
    PlannerOutput,
    Proposal,
    ToolPlan,
    WorkflowInvocation,
)
from harmony.tools.base import ToolCall
from harmony.tools.catalog import ToolCatalog
from harmony.workflow.loader import WorkflowCatalog

SYSTEM_PROMPT = """\
You are the reasoning step of an enterprise agent working on behalf of one named \
employee. You do not act. You propose, and a human decides.

Your job: given something a detector noticed and the context that could be gathered \
about it, recommend the single best response.

Rules you must follow:

1. Prefer a declared workflow when one fits. Workflows encode how this company has \
   decided the work must be done; entering one means the steps run in their declared \
   order whether or not you agree with the order. Supply its parameters accurately \
   and do not describe the steps as though you were choosing them.
2. Use individual tool calls only when no declared workflow covers the situation. \
   When you do, propose the *complete* response, not just its first step. If your \
   action changes what another team will find when they next look — a different lot \
   on the line, a different supplier on the dock, a date that moved — then telling \
   the person responsible is part of the response, not an optional extra. A half \
   plan is worse than none, because the human approves it believing it is whole.
3. Order tool calls so that anything irreversible comes last. A message cannot be \
   unsent, so send it once the changes it describes have actually been made. If an \
   earlier step fails, the ones before it can be undone and nobody has been told \
   something untrue.
4. If nothing is warranted, say so with action_kind "none" and explain why. A false \
   alarm dismissed with a reason is a good outcome.
5. Cite the specific records that led you to your conclusion. Refer to them by their \
   identifiers, exactly as they appear in the context.
6. Some systems may have been unreadable for this user. If something you would want \
   to check was not available, say so in your reasoning rather than assuming what it \
   would have contained.
7. Beliefs listed under "remembered_beliefs" are advisory background from earlier \
   runs, not current facts. They may inform how you weigh options. Never treat one \
   as evidence of the present state of a system.

Your summary will be shown to a busy manager as the first and possibly only thing \
they read. One sentence: what is wrong, and what you propose to do."""


class Planner:
    """Turns an attention item and its context into a proposal."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: ToolCatalog,
        workflows: WorkflowCatalog,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._workflows = workflows

    def plan(
        self,
        session: Session,
        memory: WorkingMemory,
        *,
        tool_patterns: list[str],
        workflow_names: list[str],
    ) -> Proposal:
        """Produce a proposal, or raise :class:`PlanRejected` if the model's answer
        names something that does not exist."""
        available_tools = self._tools.describe_for_planner(
            scopes=session.scopes, patterns=tool_patterns
        )
        available_workflows = self._workflows.describe_for_planner(workflow_names)

        output = ask(
            self._llm,
            session,
            call_site="planner",
            system=SYSTEM_PROMPT,
            prompt=self._build_prompt(session, memory, available_tools, available_workflows),
            output_model=PlannerOutput,
            max_tokens=3000,
        )
        proposal = self._to_proposal(session, output)

        session.audit.emit(
            EventType.PLAN_PROPOSED,
            proposal.summary,
            proposal_id=proposal.proposal_id,
            action_kind=output.action_kind.value,
            action=proposal.action.model_dump(mode="json"),
            reasoning=proposal.reasoning,
            alternatives_considered=proposal.alternatives_considered,
            evidence=[e.model_dump() for e in proposal.evidence],
            digest=proposal.digest()[:12],
        )
        return proposal

    # --- prompt ----------------------------------------------------------------

    def _build_prompt(
        self,
        session: Session,
        memory: WorkingMemory,
        tools: list[dict[str, Any]],
        workflows: list[dict[str, Any]],
    ) -> str:
        knowledge = memory.for_prompt()
        return f"""\
## Who you are acting for

{session.principal.name}, {session.principal.role} (id: {session.principal.id}).
Today is {session.clock.today().isoformat()}.

## What was noticed

{json.dumps(knowledge["attention_item"], indent=2, default=str)}

## Context gathered

{json.dumps(knowledge["context"], indent=2, default=str)}

## What could not be read

{json.dumps(knowledge["systems_not_readable"] + knowledge["redactions"], indent=2, default=str)}

## Advisory beliefs from earlier runs

{json.dumps(knowledge["remembered_beliefs"], indent=2, default=str)}

## Declared workflows you may enter

{json.dumps(workflows, indent=2, default=str)}

## Individual tools you may propose

{json.dumps(tools, indent=2, default=str)}

Recommend the single best response."""

    # --- conversion and validation ---------------------------------------------

    def _to_proposal(self, session: Session, output: PlannerOutput) -> Proposal:
        """Convert the model's flat answer into the typed union, rejecting anything
        that names a workflow or tool it was not offered.

        Rejection happens here rather than at the gate on purpose. The gate answers
        "is this permitted?", which presumes the plan is coherent. A plan citing a
        tool that does not exist is not a permission question at all.
        """
        if output.action_kind is ActionKind.NONE:
            action = NoAction(why=output.no_action_reason or "no reason given")
        elif output.action_kind is ActionKind.WORKFLOW:
            action = self._build_workflow_action(session, output)
        else:
            action = self._build_tool_action(session, output)

        return Proposal(
            run_id=session.run_id,
            summary=output.summary,
            reasoning=output.reasoning,
            action=action,
            evidence=output.evidence,
            alternatives_considered=output.alternatives_considered,
            created_at=session.clock.now(),
        )

    def _build_workflow_action(
        self, session: Session, output: PlannerOutput
    ) -> WorkflowInvocation:
        name = output.workflow_name
        if not name:
            raise self._reject(session, "action_kind is 'workflow' but no workflow was named")
        if not self._workflows.has(name):
            raise self._reject(
                session,
                f"no workflow named '{name}' exists",
                named=name,
                available=self._workflows.keys(),
            )

        definition = self._workflows.latest(name)
        try:
            params = definition.params_model().model_validate(output.workflow_params or {})
        except Exception as exc:
            raise self._reject(
                session,
                f"parameters for workflow '{name}' do not match its declared schema: {exc}",
                named=name,
            ) from exc

        return WorkflowInvocation(
            workflow=definition.name,
            version=definition.version,
            params=params.model_dump(mode="json"),
        )

    def _build_tool_action(self, session: Session, output: PlannerOutput) -> ToolPlan:
        calls: list[PlannedCall] = output.tool_calls or []
        if not calls:
            raise self._reject(session, "action_kind is 'tools' but no tool calls were given")

        unknown = [c.tool for c in calls if not self._tools.has(c.tool)]
        if unknown:
            raise self._reject(
                session,
                f"proposed tools that do not exist: {unknown}",
                unknown=unknown,
                available=self._tools.names(),
            )

        return ToolPlan(
            calls=[
                ToolCall(
                    tool=call.tool,
                    params=call.params,
                    step_id=f"plan:{index}",
                    rationale=call.rationale,
                )
                for index, call in enumerate(calls)
            ]
        )

    @staticmethod
    def _reject(session: Session, message: str, **details: Any) -> PlanRejected:
        session.audit.emit(
            EventType.PLAN_REJECTED, f"rejected the model's plan: {message}", **details
        )
        return PlanRejected(message, **details)
