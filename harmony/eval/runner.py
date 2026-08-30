"""Running recommendation-quality cases.

Each case builds a fresh harness from the deployment's seed data, runs detection
and planning for one user, and checks the resulting proposal against the case's
expectations. It stops before the gate: what is under test is the *recommendation*,
not the permission model, which has its own tests and does not vary with the model.

The suite is the missing half of the cassette mechanism. ``cassettes/`` freezes the
model's *output* so behaviour under test is stable; this freezes the *input* — a
seeded world and a detected situation — and asserts on the output. Run it in replay
mode and it is a regression test on the harness. Run it with ``--live`` and it is a
regression test on the model and the prompts, which is what you want before shipping
a prompt change.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import yaml

from harmony.eval.models import Check, EvalCase, EvalReport, CaseResult
from harmony.llm.client import LLMClient
from harmony.plan.models import NoAction, Proposal, ToolPlan, WorkflowInvocation
from harmony.runtime.deployment import Deployment


def load_cases(path: Path | str) -> list[EvalCase]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    return [EvalCase(**case) for case in raw]


def run_eval(
    deployment: Deployment,
    cases: list[EvalCase],
    *,
    llm: LLMClient | None = None,
    mode: str = "replay",
    start_at: str = "2026-09-02T08:00:00",
) -> EvalReport:
    """Run every case against a fresh world and report."""
    report = EvalReport(mode=mode)
    for case in cases:
        report.results.append(_run_case(deployment, case, llm=llm, start_at=start_at))
    return report


def _run_case(
    deployment: Deployment, case: EvalCase, *, llm: LLMClient | None, start_at: str
) -> CaseResult:
    from harmony.runtime.harness import Harness
    from harmony.runtime.orchestrator import Orchestrator

    result = CaseResult(case_id=case.id)
    workdir = Path(tempfile.mkdtemp(prefix=f"harmony-eval-{case.id}-"))

    try:
        harness = Harness.build(
            deployment,
            db_path=workdir / "eval.db",
            start_at=start_at,
            llm=llm,
            seed=True,
        )
        orchestrator = Orchestrator(harness)
        detections = orchestrator.detect(
            case.user, detector_ids=[case.detector] if case.detector else None
        )
        matching = [d for d in detections if case.matches in d.item.title]

        # A precision case: the detector should find nothing about this subject.
        if not case.should_detect:
            result.checks.append(
                Check(
                    name="stays silent",
                    passed=not matching,
                    detail="no attention item raised"
                    if not matching
                    else f"raised: {matching[0].item.title}",
                )
            )
            result.summary = "silent" if not matching else matching[0].item.title
            return result

        if not matching:
            result.error = (
                f"no attention item matching {case.matches!r}; "
                f"detected: {[d.item.title for d in detections]}"
            )
            return result

        run = orchestrator.run_for_item(
            matching[0].item, harness.profiles.for_user(case.user)
        )
        proposal = harness.proposals.get(run.proposal_id) if run.proposal_id else None
        if proposal is None:
            result.error = f"the run produced no proposal (state: {run.state.value})"
            return result

        result.summary = proposal.summary
        result.checks = _check(proposal, case)
        return result

    except Exception as exc:  # noqa: BLE001 - reported as a case failure
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        try:
            harness.close()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass


def _check(proposal: Proposal, case: EvalCase) -> list[Check]:
    """Every assertion the case makes, each reported separately.

    Separately, rather than as one pass/fail, because "it chose the right workflow
    but got the quantity wrong" and "it proposed doing nothing" are different
    regressions with different causes, and a single boolean hides which one you have.
    """
    expect = case.expect
    checks: list[Check] = []
    action = proposal.action

    if expect.action_kind:
        actual = action.kind.value if hasattr(action, "kind") else "?"
        checks.append(
            Check(
                name=f"action is '{expect.action_kind}'",
                passed=actual == expect.action_kind,
                detail=f"got '{actual}'",
            )
        )

    if expect.workflow:
        actual = action.workflow if isinstance(action, WorkflowInvocation) else None
        checks.append(
            Check(
                name=f"enters workflow '{expect.workflow}'",
                passed=actual == expect.workflow,
                detail=f"got {actual!r}",
            )
        )

    if expect.tools is not None:
        actual_tools = (
            [c.tool for c in action.calls] if isinstance(action, ToolPlan) else []
        )
        checks.append(
            Check(
                name="calls the expected tools in order",
                passed=actual_tools == expect.tools,
                detail=f"got {actual_tools}",
            )
        )

    for key, expected in expect.params.items():
        actual_params = (
            action.params if isinstance(action, WorkflowInvocation) else {}
        )
        got = actual_params.get(key)
        checks.append(
            Check(
                name=f"parameter {key} = {expected!r}",
                passed=str(got) == str(expected),
                detail=f"got {got!r}",
            )
        )

    haystack = _searchable(proposal)
    action_only = json.dumps(action.model_dump(mode="json"), default=str)

    for reference in expect.cites:
        checks.append(
            Check(
                name=f"cites {reference}",
                passed=reference in haystack,
                detail="not referenced in the reasoning or evidence"
                if reference not in haystack
                else "",
            )
        )

    # Checked against the *action*, not the whole proposal. Naming a trap in order
    # to reject it — "Apex is cheaper but not qualified for this part" — is the
    # behaviour we want, and a check that punished it would train the model towards
    # silent avoidance instead of stated reasoning. What must never happen is
    # acting on one.
    for forbidden in expect.forbids:
        checks.append(
            Check(
                name=f"does not act on {forbidden}",
                passed=forbidden not in action_only,
                detail=f"{forbidden} appears in the proposed action"
                if forbidden in action_only
                else "",
            )
        )

    if isinstance(action, NoAction):
        checks.append(
            Check(
                name="declines with a reason",
                passed=bool(action.why.strip()),
                detail=action.why,
            )
        )

    return checks


def _searchable(proposal: Proposal) -> str:
    """Everything the proposal says, as one string.

    Used for citation checks only. A model that names the decisive email in its
    reasoning has cited it, whether or not it also filled in the structured field —
    and a check that insisted on the structured field would be testing form rather
    than whether the conclusion rests on the right evidence.
    """
    parts: list[Any] = [
        proposal.summary,
        proposal.reasoning,
        *proposal.alternatives_considered,
        *(f"{e.source} {e.ref} {e.detail}" for e in proposal.evidence),
        json.dumps(proposal.action.model_dump(mode="json"), default=str),
    ]
    return " ".join(str(p) for p in parts)
