"""What the planner is allowed to produce.

The planner's entire output surface is a :class:`Proposal`, and a proposal's
``action`` is one of exactly three things: enter a declared workflow, run a sequence
of tool calls, or do nothing and say why.

That union is the hinge of the whole design. Scenario A's reroute is a
:class:`WorkflowInvocation` — the model decides a reroute is warranted and supplies
the parameters, and the definition takes over from there. Scenario B's lot
reallocation is a :class:`ToolPlan` — no declared workflow exists, so the model
assembles the steps. Both then travel the same road: the same gate, the same
approval, the same audit, the same execution grant. Neither path is privileged, and
adding a workflow later converts a free-form plan into a declared one without the
orchestrator noticing.

:class:`NoAction` matters more than it looks. An agent that must always act will
find something to do with a false positive. Making "nothing warranted, here is why"
a first-class answer means the planner can decline, and the declining is audited.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

from harmony.kernel.ids import digest, new_id
from harmony.tools.base import ToolCall


class ActionKind(StrEnum):
    WORKFLOW = "workflow"
    TOOLS = "tools"
    NONE = "none"


class WorkflowInvocation(BaseModel):
    """Enter a declared workflow with these parameters.

    The model chooses *whether* and supplies *what*. It does not supply steps, their
    order, or their number — those come from the definition, which is versioned and
    owned by the business.
    """

    kind: Literal[ActionKind.WORKFLOW] = ActionKind.WORKFLOW
    workflow: str
    version: int
    params: dict[str, Any] = Field(default_factory=dict)

    def describe(self) -> str:
        return f"workflow {self.workflow}@v{self.version}"


class ToolPlan(BaseModel):
    """Run these tool calls, in this order.

    The free-form path. Every call is still validated against the catalog, still
    scope-checked, still gated and still covered by a grant — the difference from a
    workflow is that the sequence was assembled by the model rather than declared,
    and so it gets no resumption or compensation guarantees. DESIGN.md argues this
    asymmetry is the design's main weakness.
    """

    kind: Literal[ActionKind.TOOLS] = ActionKind.TOOLS
    calls: list[ToolCall] = Field(default_factory=list)

    def describe(self) -> str:
        return " → ".join(call.tool for call in self.calls) or "no calls"


class NoAction(BaseModel):
    """Nothing is warranted. The reason is recorded."""

    kind: Literal[ActionKind.NONE] = ActionKind.NONE
    why: str

    def describe(self) -> str:
        return "no action"


ProposedAction = Annotated[
    Union[WorkflowInvocation, ToolPlan, NoAction], Field(discriminator="kind")
]


class EvidenceRef(BaseModel):
    """A citation from the proposal back into what the agent saw."""

    source: str
    ref: str
    detail: str = ""


class Proposal(BaseModel):
    """A recommendation, its reasoning, and the action it implies."""

    proposal_id: str = Field(default_factory=lambda: new_id("PROP"))
    run_id: str = ""
    summary: str
    """The sentence a human reads in the approval prompt."""

    reasoning: str
    """Why, at length. Shown on request and always audited."""

    action: ProposedAction
    evidence: list[EvidenceRef] = Field(default_factory=list)
    alternatives_considered: list[str] = Field(default_factory=list)
    created_at: _dt.datetime | None = None

    def digest(self) -> str:
        """Stable identity of *what will happen*.

        Deliberately over the action and summary only. Re-wording the reasoning does
        not invalidate an approval; changing a supplier, a quantity or a step does.
        This is what an approval is bound to.
        """
        return digest(self.summary, self.action.model_dump(mode="json"))

    @property
    def is_actionable(self) -> bool:
        return not isinstance(self.action, NoAction)

    def describe(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "summary": self.summary,
            "action": self.action.describe(),
            "digest": self.digest()[:12],
        }


# --- the model's output shape --------------------------------------------------


class PlannedCall(BaseModel):
    """One tool call as the model proposes it."""

    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class PlannerOutput(BaseModel):
    """The schema the planner model is required to fill in.

    Flat rather than a discriminated union: tool-use schemas are more reliably
    filled when the branch is an enum field and the branches are separate optional
    objects. :meth:`to_proposal` converts to the typed union and rejects
    inconsistent combinations, so the looseness stops at this boundary.
    """

    summary: str = Field(
        description="One sentence for the human: the problem and what you propose."
    )
    reasoning: str = Field(
        description="Why you concluded this, referring to specific records you were shown."
    )
    action_kind: ActionKind = Field(
        description=(
            "'workflow' to enter a declared workflow, 'tools' to propose individual "
            "tool calls, 'none' if no action is warranted."
        )
    )
    workflow_name: str | None = Field(
        default=None, description="Required when action_kind is 'workflow'."
    )
    workflow_params: dict[str, Any] | None = Field(
        default=None, description="Parameters for the workflow, matching its declared schema."
    )
    tool_calls: list[PlannedCall] | None = Field(
        default=None, description="Required when action_kind is 'tools'."
    )
    no_action_reason: str | None = Field(
        default=None, description="Required when action_kind is 'none'."
    )
    evidence: list[EvidenceRef] = Field(
        default_factory=list, description="Records that justify this, cited by id."
    )
    alternatives_considered: list[str] = Field(
        default_factory=list, description="Options you weighed and set aside, and why."
    )
