"""What a recommendation-quality case asserts, and what running one produces.

The hard part of evaluating an agent is that the interesting output is prose, and
prose does not compare. Asserting on exact text gives a suite that fails whenever a
prompt is reworded, which is a suite people delete.

So a case asserts on *properties of the decision* rather than on its wording:

* did it reach for the right kind of action — a declared workflow, individual
  tools, or nothing at all?
* did it name the right workflow, or the right tools?
* did it supply the parameters the situation implies?
* did it cite the evidence the conclusion actually rests on?
* did it avoid the things it must never do?

Every one of those survives a rewrite and fails on a regression, which is the only
combination worth having. Wording quality is a separate question, and DESIGN.md
says how it would be measured (human edit distance on drafted text, and a judge
model calibrated against labels) rather than pretending this suite covers it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Expectation(BaseModel):
    """What a correct answer looks like, expressed as properties.

    Every field is optional. A case asserts only what it is actually about, so a
    case testing "does it decline a false positive" does not have to restate the
    whole shape of a correct reroute.
    """

    action_kind: str | None = Field(
        default=None, description="workflow, tools, or none."
    )
    workflow: str | None = None
    tools: list[str] | None = Field(
        default=None, description="Tools the plan must call, in order."
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="Workflow parameters that must match."
    )
    cites: list[str] = Field(
        default_factory=list,
        description="Record ids the reasoning or evidence must refer to.",
    )
    forbids: list[str] = Field(
        default_factory=list,
        description="Strings that must appear nowhere in the proposal — traps.",
    )


class EvalCase(BaseModel):
    """One frozen situation and the decision it should produce."""

    id: str
    description: str = ""
    user: str
    detector: str | None = Field(
        default=None, description="Run only this detector. Defaults to the profile's."
    )
    matches: str = Field(
        description="Substring identifying which attention item this case is about."
    )
    expect: Expectation = Field(default_factory=Expectation)
    should_detect: bool = Field(
        default=True,
        description="False asserts the detector stays silent — a precision case.",
    )


class Check(BaseModel):
    """One assertion, and what actually happened."""

    name: str
    passed: bool
    detail: str = ""


class CaseResult(BaseModel):
    """The outcome of one case."""

    case_id: str
    checks: list[Check] = Field(default_factory=list)
    summary: str = ""
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


class EvalReport(BaseModel):
    """Everything that ran."""

    results: list[CaseResult] = Field(default_factory=list)
    mode: str = "replay"

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    @property
    def ok(self) -> bool:
        return self.failed == 0
