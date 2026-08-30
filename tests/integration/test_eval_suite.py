"""The recommendation-quality suite, run as part of the test suite.

Keeping it here means a change that degrades the agent's *judgment* — rather than
its correctness — fails the build like anything else. Recommendation quality is the
thing most likely to rot silently, because nothing throws when an agent starts
giving worse advice.

In replay mode this asserts the harness still turns a frozen world into the same
decision. `harmony eval --live` runs the identical cases against the real model,
which is the check to run before shipping a prompt change.
"""

from __future__ import annotations

import pytest

from harmony.eval.runner import load_cases, run_eval
from harmony.llm.client import StubClient
from northfield import DEPLOYMENT
from northfield.demo.scripted_answers import SCRIPTED_ANSWERS

pytestmark = pytest.mark.integration


def _report():
    cases = load_cases(DEPLOYMENT.eval_cases_path)
    return cases, run_eval(
        DEPLOYMENT, cases, llm=StubClient(dict(SCRIPTED_ANSWERS)), mode="stub"
    )


def test_every_golden_case_passes():
    cases, report = _report()
    assert report.results, "no cases ran"

    failures = [
        f"{r.case_id}: " + (r.error or "; ".join(c.name for c in r.failures))
        for r in report.results
        if not r.passed
    ]
    assert not failures, "recommendation quality regressed:\n  " + "\n  ".join(failures)


def test_the_suite_covers_both_planning_paths_and_precision():
    """A suite of only positive cases would pass an agent that alerts on everything.

    Asserting the shape of the suite, rather than only its result, is what stops it
    quietly becoming that.
    """
    cases, _ = _report()
    kinds = {c.expect.action_kind for c in cases if c.should_detect}

    assert "workflow" in kinds, "no case exercises the declared-workflow path"
    assert "tools" in kinds, "no case exercises the free-form path"
    assert any(not c.should_detect for c in cases), "no case asserts silence"


def test_every_case_asserts_something():
    """A case with no expectations passes unconditionally and is worse than absent."""
    cases, _ = _report()
    for case in cases:
        if not case.should_detect:
            continue
        expect = case.expect
        assert any(
            [expect.action_kind, expect.workflow, expect.tools, expect.params, expect.cites]
        ), f"case '{case.id}' asserts nothing"
