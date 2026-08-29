"""Loading and validating workflow definitions.

Every check in this module exists so that a broken definition fails at start-up
rather than halfway through a reroute. A workflow that discovers at step 5 that its
compensation tool was misspelled has already created a purchase order it can no
longer withdraw, and no amount of runtime error handling recovers the position.

The checks, and what each one prevents:

===============================  =============================================
unique step ids                  results silently overwriting each other
tool exists                      a mid-run ``NotRegistered``
compensation exists and writes   a rollback path that cannot roll back
writes declare rollback intent   an author who never considered failure
bindings point backwards         a step reading a result that does not exist yet
llm steps declare a schema       an unbounded model call inside a fixed workflow
llm steps never write            the model acquiring authority the definition
                                 did not grant it
===============================  =============================================

The last two are the load-bearing ones for Part 2. They are what make "LLM steps
are allowed but bounded" a property the loader enforces rather than a convention
the author is trusted to follow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from harmony.kernel.errors import WorkflowDefinitionInvalid
from harmony.kernel.registry import Registry
from harmony.tools.catalog import ToolCatalog
from harmony.workflow.bindings import referenced_paths
from harmony.workflow.models import StepKind, WorkflowDefinition


class WorkflowCatalog:
    """Definitions, keyed by ``name@vN``."""

    def __init__(self) -> None:
        self._registry: Registry[WorkflowDefinition] = Registry("workflow")

    def add(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        return self._registry.register(definition.key, definition)

    def get(self, name: str, version: int) -> WorkflowDefinition:
        return self._registry.get(f"{name}@v{version}")

    def latest(self, name: str) -> WorkflowDefinition:
        matches = [d for _, d in self._registry if d.name == name]
        if not matches:
            raise WorkflowDefinitionInvalid(f"no workflow named '{name}'", workflow=name)
        return max(matches, key=lambda d: d.version)

    def has(self, name: str, version: int | None = None) -> bool:
        if version is not None:
            return f"{name}@v{version}" in self._registry
        return any(d.name == name for _, d in self._registry)

    def keys(self) -> list[str]:
        return self._registry.names()

    def all(self) -> list[WorkflowDefinition]:
        return self._registry.all()

    def describe_for_planner(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        """Workflows the profile permits, as the planner is shown them."""
        definitions = self.all()
        if allowed is not None:
            permitted = set(allowed)
            definitions = [
                d for d in definitions if d.name in permitted or d.key in permitted
            ]
        return [d.describe_for_planner() for d in definitions]


def load_definition(path: Path | str, *, catalog: ToolCatalog) -> WorkflowDefinition:
    """Parse and validate one definition file."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise WorkflowDefinitionInvalid(f"{path} does not contain a mapping", path=str(path))

    try:
        definition = WorkflowDefinition(**raw, source_path=str(path))
    except Exception as exc:
        raise WorkflowDefinitionInvalid(
            f"{path} is not a valid workflow definition: {exc}", path=str(path)
        ) from exc

    problems = validate_definition(definition, catalog=catalog)
    if problems:
        raise WorkflowDefinitionInvalid(
            f"{path} failed validation:\n  - " + "\n  - ".join(problems),
            path=str(path),
            problems=problems,
        )
    return definition


def load_directory(directory: Path | str, *, catalog: ToolCatalog) -> WorkflowCatalog:
    """Load every ``*.yaml`` in a directory into a catalog."""
    workflows = WorkflowCatalog()
    for path in sorted(Path(directory).glob("*.yaml")):
        workflows.add(load_definition(path, catalog=catalog))
    return workflows


def validate_definition(
    definition: WorkflowDefinition, *, catalog: ToolCatalog
) -> list[str]:
    """Return every problem found. Empty means the definition is sound."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    available_paths: set[str] = {f"params.{name}" for name in definition.params}

    if not definition.steps:
        problems.append("definition declares no steps")

    for index, step in enumerate(definition.steps):
        where = f"step {index} ('{step.id}')"

        if step.id in seen_ids:
            problems.append(f"{where}: duplicate step id")
        seen_ids.add(step.id)

        forward_sources: list[Any] = [step.input, step.prompt, step.must_mention]
        forward_sources.extend(
            spec.enum_from for spec in step.output_schema.values() if spec.enum_from
        )
        problems.extend(_check_bindings(forward_sources, where, available_paths))

        # A compensation runs only after its own step completed, so it may read
        # that step's output as well as everything before it. This is the one place
        # a binding legitimately points at the step it belongs to.
        if step.compensation:
            problems.extend(
                _check_bindings(
                    [step.compensation.input],
                    f"{where} compensation",
                    available_paths | {f"steps.{step.id}.output"},
                )
            )

        if step.kind is StepKind.TOOL:
            problems.extend(_check_tool_step(step, where, catalog))
        else:
            problems.extend(_check_llm_step(step, where, catalog))

        problems.extend(_check_compensation(step, where, catalog))

        # Outputs become available to later steps only.
        available_paths.add(f"steps.{step.id}.output")

    return problems


def _check_bindings(sources: list[Any], where: str, available: set[str]) -> list[str]:
    """Every binding must point at a parameter or an already-available step output."""
    problems: list[str] = []

    for path in {p for source in sources for p in referenced_paths(source)}:
        root = path.split(".")[0]
        if root == "clock":
            continue
        if root == "params":
            if path.split(".")[0] + "." + path.split(".")[1] not in available:
                problems.append(f"{where}: '${{{path}}}' names a parameter that is not declared")
            continue
        if root == "steps":
            prefix = ".".join(path.split(".")[:3])  # steps.<id>.output
            if prefix not in available:
                problems.append(
                    f"{where}: '${{{path}}}' reads a step that has not run yet "
                    "(or does not exist)"
                )
            continue
        problems.append(f"{where}: '${{{path}}}' uses an unknown namespace '{root}'")
    return problems


def _check_tool_step(step, where: str, catalog: ToolCatalog) -> list[str]:
    problems: list[str] = []
    if not step.tool:
        problems.append(f"{where}: tool steps must name a tool")
        return problems
    if not catalog.has(step.tool):
        problems.append(f"{where}: tool '{step.tool}' is not registered")
        return problems

    spec = catalog.get(step.tool)
    if spec.writes and step.compensation is None and not step.irreversible:
        problems.append(
            f"{where}: '{step.tool}' writes but declares neither a compensation nor "
            "irreversible: true — say which, so rollback is a decision and not an oversight"
        )
    return problems


def _check_llm_step(step, where: str, catalog: ToolCatalog) -> list[str]:
    """LLM steps are bounded by construction: a declared schema, and no writes."""
    problems: list[str] = []
    if step.tool:
        problems.append(
            f"{where}: llm steps may not name a tool — a model step never writes. "
            "Put the write in its own tool step so it is scoped, gated and audited."
        )
    if not step.output_schema:
        problems.append(
            f"{where}: llm steps must declare output_schema; an unconstrained model "
            "answer inside a fixed workflow defeats the point of fixing it"
        )
    if not step.prompt:
        problems.append(f"{where}: llm steps must declare a prompt")
    if step.compensation:
        problems.append(f"{where}: llm steps have no effects, so cannot be compensated")
    return problems


def _check_compensation(step, where: str, catalog: ToolCatalog) -> list[str]:
    problems: list[str] = []
    if step.compensation is None:
        return problems
    name = step.compensation.tool
    if not catalog.has(name):
        problems.append(f"{where}: compensation tool '{name}' is not registered")
        return problems
    if not catalog.get(name).writes:
        problems.append(
            f"{where}: compensation tool '{name}' is not declared as a write, so it "
            "cannot undo anything"
        )
    return problems
