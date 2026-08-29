"""Gate vocabulary: verdicts, rule results, and what a rule gets to look at.

The gate is where the harness stops trusting the model. Everything upstream —
detection, context, planning — produces a *proposal*, and a proposal is a claim
about what should happen. The gate decides what is permitted, in code, from data,
with every rule's reasoning recorded.

Three verdicts and no more. In particular there is no "warn" or "allow with
conditions": a decision an operator has to interpret is a decision nobody made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from harmony.identity.directory import UserDirectory
from harmony.identity.session import Session
from harmony.plan.models import Proposal
from harmony.providers.base import ContextBundle
from harmony.tools.base import ToolSpec

if TYPE_CHECKING:
    from harmony.workflow.models import WorkflowDefinition


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class RuleVerdict(BaseModel):
    """One rule's answer, with the reasoning that produced it."""

    rule_id: str
    verdict: Verdict
    reason: str
    approver_id: str | None = None
    """Who must approve. Only meaningful for ``REQUIRE_APPROVAL``."""

    details: dict[str, Any] = Field(default_factory=dict)
    """The inputs the rule actually used. Audited, so "why did this need approval?"
    is answerable from the ledger without re-running anything."""

    @classmethod
    def allow(cls, rule_id: str, reason: str, **details: Any) -> RuleVerdict:
        return cls(rule_id=rule_id, verdict=Verdict.ALLOW, reason=reason, details=details)

    @classmethod
    def deny(cls, rule_id: str, reason: str, **details: Any) -> RuleVerdict:
        return cls(rule_id=rule_id, verdict=Verdict.DENY, reason=reason, details=details)

    @classmethod
    def approval(
        cls, rule_id: str, reason: str, *, approver_id: str, **details: Any
    ) -> RuleVerdict:
        return cls(
            rule_id=rule_id,
            verdict=Verdict.REQUIRE_APPROVAL,
            reason=reason,
            approver_id=approver_id,
            details=details,
        )


@dataclass(frozen=True)
class GateContext:
    """Everything a rule may consider.

    Passed whole to every rule so that adding a rule which needs a new input is a
    change to this class rather than to the pipeline's call signature — and so that
    a rule cannot quietly reach for something outside it.

    A dataclass rather than a pydantic model: it carries live objects — a session,
    the directory, resolved tool specs — that are constructed rather than parsed.
    Everything crossing a trust boundary in this codebase is a pydantic model;
    nothing here does.
    """

    session: Session
    proposal: Proposal
    tools: list[ToolSpec]
    """Every tool the proposal will invoke, resolved from the catalog. For a
    workflow this comes from the definition, not from the model."""

    directory: UserDirectory
    definition: "WorkflowDefinition | None" = None
    context: ContextBundle = field(default_factory=ContextBundle)
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def write_tools(self) -> list[ToolSpec]:
        return [t for t in self.tools if t.writes]

    @property
    def required_scopes(self) -> frozenset[str]:
        return frozenset().union(*(t.scopes for t in self.tools)) if self.tools else frozenset()

    def tool_params(self, tool_name: str) -> list[dict[str, Any]]:
        """Parameters the proposal supplies to a named tool.

        Only meaningful on the free-form path, where the model supplies them. Inside
        a workflow, parameters come from bindings resolved at run time, so a rule
        that needs to inspect a value there must read it from ``proposal.action``
        params or from context instead.
        """
        from harmony.plan.models import ToolPlan

        if not isinstance(self.proposal.action, ToolPlan):
            return []
        return [c.params for c in self.proposal.action.calls if c.tool == tool_name]


class GateDecision(BaseModel):
    """The composed outcome of every rule."""

    verdict: Verdict
    approver_id: str | None = None
    rule_verdicts: list[RuleVerdict] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @property
    def denied(self) -> bool:
        return self.verdict is Verdict.DENY

    @property
    def needs_approval(self) -> bool:
        return self.verdict is Verdict.REQUIRE_APPROVAL

    def denials(self) -> list[RuleVerdict]:
        return [v for v in self.rule_verdicts if v.verdict is Verdict.DENY]

    def summary(self) -> str:
        if self.denied:
            return "; ".join(v.reason for v in self.denials())
        if self.needs_approval:
            return "; ".join(
                v.reason for v in self.rule_verdicts if v.verdict is Verdict.REQUIRE_APPROVAL
            )
        return "permitted without approval"
