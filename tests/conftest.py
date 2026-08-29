"""Shared fixtures.

Every test builds a real harness against a real (temporary) database with a real
simulated clock. Nothing is mocked except the model, and that is replaced by the
scripted client rather than a mock so the tests exercise the same validation,
guardrails and audit path a live run would.

The alternative — unit tests against hand-built objects — would pass while the
wiring was broken, and the wiring is most of what this codebase is.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from harmony.identity.grant import ExecutionGrant
from harmony.llm.client import StubClient
from harmony.runtime.harness import Harness
from harmony.runtime.orchestrator import Orchestrator
from northfield import DEPLOYMENT
from northfield.demo.scripted_answers import SCRIPTED_ANSWERS

START = "2026-09-02T08:00:00"


@pytest.fixture
def llm() -> StubClient:
    """The scripted model. Raises on any call site it has no answer for, so a test
    that unexpectedly reaches the model fails loudly rather than silently."""
    return StubClient(dict(SCRIPTED_ANSWERS))


@pytest.fixture
def harness(tmp_path, llm) -> Harness:
    """A fully wired harness on a fresh database at 2026-09-02."""
    built = Harness.build(
        DEPLOYMENT,
        db_path=tmp_path / "test.db",
        start_at=START,
        llm=llm,
        seed=True,
    )
    yield built
    built.close()


@pytest.fixture
def orchestrator(harness) -> Orchestrator:
    return Orchestrator(harness)


@pytest.fixture
def dana_session(harness):
    """A session for the purchasing manager, scoped by her profile."""
    profile = harness.profiles.for_user("u-101")
    return harness.user_session(
        "u-101", run_id="RUN-test", profile_scopes=profile.scope_set(), purpose="test"
    )


@pytest.fixture
def open_grant():
    """A grant permitting any tool. For tests exercising something other than
    approval — the grant machinery has its own tests."""

    def _grant(*tools: str) -> ExecutionGrant:
        return ExecutionGrant(
            proposal_digest="test-digest",
            granted_by="u-101",
            granted_at=_dt.datetime(2026, 9, 2, 9, 0),
            allowed_tools=frozenset(tools),
            approval_id="APR-test",
            reason="test",
        )

    return _grant


@pytest.fixture
def shortfall_item(orchestrator):
    """The Scenario A attention item, detected the way a real run detects it."""

    def _detect(user_id: str = "u-101"):
        results = [r for r in orchestrator.detect(user_id) if "4812" in r.item.title]
        assert results, "expected the material_shortfall detector to raise for 4812"
        return results[0].item

    return _detect
