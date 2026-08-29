"""Workflow definitions: the business's step order, expressed as data.

Purchasing's requirement was blunt — *"We don't want the AI improvising a PO
reroute. The steps are fixed. In that order. Every time."* So the order lives in a
versioned YAML file that a purchasing analyst can read, and the engine in
:mod:`harmony.workflow.engine` is an interpreter with no opinion about what the
steps mean.

Two things a reader should notice in the shipped definition:

**Steps are ordered by reversibility as well as by business logic.** Every
compensable write precedes the one irreversible effect (the notification to
production). That is not incidental — it is what makes rollback meaningful. A plan
that notifies first and creates the PO second is *the same business steps* and is
much worse, because the failure case leaves a supervisor acting on a message about
a purchase order that does not exist.

**LLM steps are bounded by their neighbours.** ``choose_supplier`` looks like the
model making a purchasing decision. It is not: a deterministic step computed the
candidate list, the schema restricts the answer to that list, and the justification
is recorded as prose for a human rather than consumed as logic. The model's freedom
is exactly one choice among options code produced.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from harmony.kernel.errors import WorkflowDefinitionInvalid


class StepKind(StrEnum):
    TOOL = "tool"
    """Invoke a named tool. The only kind that may touch a system of record."""

    LLM = "llm"
    """Ask the model a bounded question. May never write; the engine enforces it."""


class OnEmpty(StrEnum):
    FAIL = "fail"
    """No results means the workflow's precondition does not hold. Stop.

    This is how "confirm the alternate supplier is approved for the part" becomes
    an enforced gate rather than a hopeful lookup: if the filter returns nothing,
    there is no reroute to make and the run must say so."""

    CONTINUE = "continue"


class FieldSpec(BaseModel):
    """One field of an LLM step's answer.

    A deliberately small schema language. It covers what the shipped workflows
    need and nothing more; the README explains why building an expression language
    here would have been a mistake.
    """

    type: Literal["string", "integer", "number", "boolean", "date"] = "string"
    description: str = ""
    max_length: int | None = None
    enum_from: str | None = None
    """Names a step output holding the permitted values, e.g.
    ``steps.filter_by_lead_time.output.supplier_ids``. Resolved at run time and
    enforced as a guardrail — the model picks from a list code computed."""

    def python_type(self) -> type:
        return {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "date": _dt.date,
        }[self.type]


class Compensation(BaseModel):
    """How to undo a step, if it can be undone."""

    tool: str
    input: dict[str, Any] = Field(default_factory=dict)


class Step(BaseModel):
    """One node in the graph."""

    id: str
    kind: StepKind
    description: str = ""

    # tool steps
    tool: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    on_empty: OnEmpty = OnEmpty.CONTINUE
    empty_check: str | None = None
    """Which output field ``on_empty`` inspects. Defaults to the first field."""

    # llm steps
    system: str = ""
    prompt: str = ""
    output_schema: dict[str, FieldSpec] = Field(default_factory=dict)
    must_mention: list[str] = Field(default_factory=list)
    """Bindings whose resolved values must appear in the answer's text. Keeps a
    drafted notification anchored to the facts it is about."""

    compensation: Compensation | None = None
    irreversible: bool = False
    """Set when a step has no compensation *by nature* rather than by omission.
    Declaring it is what lets the loader reject a write step whose author simply
    forgot to think about rollback."""

    def output_model(self, name_prefix: str, enum_values: dict[str, list[str]]) -> type[BaseModel]:
        """Build the pydantic model this LLM step's answer must satisfy."""
        fields: dict[str, Any] = {}
        for field_name, spec in self.output_schema.items():
            if spec.enum_from:
                allowed = enum_values.get(field_name, [])
                if not allowed:
                    raise WorkflowDefinitionInvalid(
                        f"step '{self.id}' field '{field_name}' draws its options from "
                        f"'{spec.enum_from}', which resolved to nothing",
                        step=self.id,
                        field=field_name,
                    )
                constraints = Field(description=spec.description or f"one of {allowed}")
                fields[field_name] = (Literal[tuple(allowed)], constraints)  # type: ignore[valid-type]
            else:
                constraints = Field(
                    description=spec.description or "",
                    max_length=spec.max_length if spec.type == "string" else None,
                )
                fields[field_name] = (spec.python_type(), constraints)
        return create_model(f"{name_prefix}_{self.id}_output", **fields)


class ParamSpec(BaseModel):
    """One workflow parameter, as the planner must supply it."""

    type: Literal["string", "integer", "number", "boolean", "date"] = "string"
    description: str = ""
    required: bool = True


class WorkflowDefinition(BaseModel):
    """A named, versioned, ordered sequence of steps."""

    name: str
    version: int
    description: str = ""
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)
    source_path: str = ""

    @property
    def key(self) -> str:
        return f"{self.name}@v{self.version}"

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise WorkflowDefinitionInvalid(
            f"workflow '{self.key}' has no step '{step_id}'", workflow=self.key
        )

    def tool_names(self) -> frozenset[str]:
        """Every tool this definition can invoke, including compensations.

        This is what an execution grant is populated with: approving a reroute
        authorises the reroute's tools and nothing else, even where the principal's
        scopes would permit more.
        """
        names = {step.tool for step in self.steps if step.tool}
        names |= {step.compensation.tool for step in self.steps if step.compensation}
        return frozenset(n for n in names if n)

    def params_model(self) -> type[BaseModel]:
        """The pydantic model the planner's parameters are validated against."""
        fields: dict[str, Any] = {}
        for name, spec in self.params.items():
            python_type = {
                "string": str,
                "integer": int,
                "number": float,
                "boolean": bool,
                "date": _dt.date,
            }[spec.type]
            if spec.required:
                fields[name] = (python_type, Field(description=spec.description))
            else:
                fields[name] = (python_type | None, Field(default=None, description=spec.description))
        return create_model(f"{self.name}_v{self.version}_params", **fields)

    def describe_for_planner(self) -> dict[str, Any]:
        """How the workflow is offered to the model.

        Steps are described but not made editable: the model is told what entering
        this workflow will cause, so it can judge whether that is the right
        response, and is given no way to change it.
        """
        return {
            "workflow": self.name,
            "version": self.version,
            "description": self.description,
            "parameters": self.params_model().model_json_schema(),
            "steps_that_will_run": [
                {"id": s.id, "description": s.description or s.tool or s.kind.value}
                for s in self.steps
            ],
        }


class InstanceStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"
    """Partial effects remain in place and a human must intervene. Loud on purpose:
    this is the state an operator must never be able to overlook."""


class WorkflowInstance(BaseModel):
    """One in-flight or finished execution of a definition."""

    instance_id: str
    run_id: str
    definition_name: str
    definition_version: int
    """Pinned at start. An instance runs the definition it began with, even if a
    newer version is published mid-flight; DESIGN.md covers migration."""

    params: dict[str, Any] = Field(default_factory=dict)
    status: InstanceStatus = InstanceStatus.RUNNING
    cursor: int = 0
    """Index of the next step to run. Advanced in the same transaction that records
    the step's result, which is what makes resume exact."""

    step_results: dict[str, Any] = Field(default_factory=dict)
    compensation_log: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    created_at: _dt.datetime | None = None
    updated_at: _dt.datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.definition_name}@v{self.definition_version}"
