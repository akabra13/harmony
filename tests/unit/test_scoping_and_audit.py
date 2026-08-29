"""The two chokepoints, and the ledger that records them.

Scope enforcement lives in exactly two places — the context broker for reads and the
tool invoker for writes — and the value of that concentration is that these tests
cover every provider and every tool at once.
"""

from __future__ import annotations

import pytest

from harmony.audit.models import EventType
from harmony.identity.session import Session
from harmony.kernel.errors import ApprovalRequired, PlanRejected, ScopeDenied
from harmony.providers.base import ContextRequest, SubjectRef
from harmony.tools.base import ToolCall

# Any event type will do for the tamper tests; the ledger does not care which.
_SOME_EVENT = EventType.RUN_STARTED


# --- session scoping -----------------------------------------------------------


def test_the_effective_scope_set_is_an_intersection(harness):
    """A profile can only narrow. There is no code path that widens a live session,
    which is what stops a compromised prompt from acquiring permissions."""
    dana = harness.directory.get("u-101")
    assert "mail:send" in dana.scopes

    profile = harness.profiles.for_user("u-101")
    assert "mail:send" not in profile.scopes

    session = harness.user_session(
        "u-101", run_id="RUN-s", profile_scopes=profile.scope_set(), purpose="test"
    )
    assert not session.can("mail:send")
    assert session.can("erp:po:create")


def test_downscoping_can_only_narrow(harness, dana_session):
    narrowed = dana_session.downscope(
        frozenset({"erp:po:read", "quality:lot:allocate"}), purpose="test"
    )
    assert narrowed.can("erp:po:read")
    assert not narrowed.can("erp:po:create")
    assert not narrowed.can("quality:lot:allocate"), "a scope Dana never held"


def test_a_scope_denial_is_audited_before_it_is_raised(harness, dana_session):
    """An agent that quietly declined would be worse than one that acted wrongly —
    nobody would know to look."""
    with pytest.raises(ScopeDenied):
        dana_session.require("quality:lot:allocate", subject="reallocate a lot")

    event = next(
        e
        for e in harness.audit_log.for_run("RUN-test")
        if e.event_type.value == "gate.scope_denied"
    )
    assert "quality:lot:allocate" in event.payload["missing"]
    assert event.payload["subject"] == "reallocate a lot"


# --- provider scoping ----------------------------------------------------------


def test_a_provider_is_skipped_when_the_session_cannot_reach_it(harness):
    """And the bundle says so, rather than looking empty."""
    profile = harness.profiles.for_user("u-303")
    session = harness.user_session(
        "u-303", run_id="RUN-p", profile_scopes=profile.scope_set(), purpose="test"
    )
    bundle = harness.broker.gather(
        session,
        ContextRequest(purpose="test", subjects=[SubjectRef(kind="part", id="P-1188")]),
        systems=["erp", "quality"],
    )

    assert bundle.system("erp") is not None
    assert bundle.system("quality") is None
    unreachable = {u["system"]: u for u in bundle.unreachable}
    assert "quality:lot:read" in unreachable["quality"]["missing_scopes"]


def test_the_planner_is_told_what_it_could_not_see(harness):
    """So it can say "I could not check" rather than assuming nothing was there."""
    from harmony.memory.working import WorkingMemory
    from harmony.detect.models import AttentionItem

    profile = harness.profiles.for_user("u-303")
    session = harness.user_session(
        "u-303", run_id="RUN-q", profile_scopes=profile.scope_set(), purpose="test"
    )
    bundle = harness.broker.gather(
        session, ContextRequest(purpose="test"), systems=["erp", "quality"]
    )
    memory = WorkingMemory(
        item=AttentionItem.build(
            detector_id="d", principal_id="u-303", title="t", subjects=[], facts={}
        ),
        context=bundle,
    )
    assert memory.for_prompt()["systems_not_readable"]


def test_mail_returns_only_messages_the_user_is_a_party_to(harness, dana_session):
    """M-007 is between two other people. Dana's agent must not see it, and the
    ledger must record that it was withheld rather than absent."""
    bundle = harness.broker.gather(
        dana_session, ContextRequest(purpose="test"), systems=["mail"]
    )
    slice_ = bundle.system("mail")
    ids = {m["message_id"] for m in slice_.get("messages")}

    assert "M-001" in ids
    assert "M-007" not in ids
    assert slice_.redacted_count() >= 1


def test_redactions_reach_the_ledger(harness, dana_session):
    """"What the agent saw" means the visibility boundary, not only the contents."""
    harness.broker.gather(dana_session, ContextRequest(purpose="test"), systems=["mail"])
    assert any(
        e.event_type.value == "context.redacted"
        for e in harness.audit_log.for_run("RUN-test")
    )


# --- the write chokepoint ------------------------------------------------------


def test_an_unregistered_tool_is_refused_before_authorisation(harness, dana_session):
    """A plan citing a tool that does not exist is not a permission question."""
    with pytest.raises(PlanRejected, match="no tool named"):
        harness.invoker.invoke(
            dana_session, ToolCall(tool="erp.wire_money", params={}, step_id="x")
        )


def test_invalid_parameters_are_refused(harness, dana_session, open_grant):
    with pytest.raises(PlanRejected, match="invalid"):
        harness.invoker.invoke(
            dana_session,
            ToolCall(
                tool="erp.create_purchase_order",
                params={"part_id": "P-4471", "qty": -5},
                step_id="x",
            ),
            grant=open_grant("erp.create_purchase_order"),
        )


def test_a_write_without_a_grant_is_refused(harness, dana_session):
    """Scopes say what a person may do in general; a grant says what a human agreed
    to this time. Holding the first is not holding the second."""
    assert dana_session.can("erp:po:create")

    with pytest.raises(ApprovalRequired, match="execution grant"):
        harness.invoker.invoke(
            dana_session,
            ToolCall(
                tool="erp.create_purchase_order",
                params={
                    "part_id": "P-4471",
                    "supplier_id": "S-Z",
                    "qty": 10,
                    "need_by": "2026-09-07",
                },
                step_id="x",
            ),
        )


def test_a_grant_for_a_different_tool_does_not_transfer(harness, dana_session, open_grant):
    with pytest.raises(ApprovalRequired, match="not covered"):
        harness.invoker.invoke(
            dana_session,
            ToolCall(tool="erp.cancel_purchase_order", params={"po_id": "PO-77812"}, step_id="x"),
            grant=open_grant("erp.create_purchase_order"),
        )


def test_reads_need_no_grant(harness, dana_session):
    result = harness.invoker.invoke(
        dana_session,
        ToolCall(
            tool="erp.list_approved_suppliers_for_part",
            params={"part_id": "P-4471"},
            step_id="x",
        ),
    )
    assert result.ok
    assert "S-Z" in result.output["supplier_ids"]


def test_the_same_write_twice_happens_once(harness, dana_session, open_grant):
    grant = open_grant("erp.create_purchase_order")
    call = ToolCall(
        tool="erp.create_purchase_order",
        params={
            "part_id": "P-4471",
            "supplier_id": "S-Z",
            "qty": 10,
            "need_by": "2026-09-07",
        },
        step_id="idem-1",
    )

    first = harness.invoker.invoke(dana_session, call, grant=grant)
    second = harness.invoker.invoke(dana_session, call, grant=grant)

    assert not first.replayed and second.replayed
    assert first.output["po_id"] == second.output["po_id"]

    from northfield.systems import erp

    created = [
        p for p in erp.list_purchase_orders(harness.store, part_id="P-4471") if p["qty"] == 10
    ]
    assert len(created) == 1


def test_a_different_step_is_a_different_write(harness, dana_session, open_grant):
    """Idempotency must not collapse two genuinely distinct orders."""
    grant = open_grant("erp.create_purchase_order")
    params = {
        "part_id": "P-4471",
        "supplier_id": "S-Z",
        "qty": 10,
        "need_by": "2026-09-07",
    }
    first = harness.invoker.invoke(
        dana_session,
        ToolCall(tool="erp.create_purchase_order", params=params, step_id="step-a"),
        grant=grant,
    )
    second = harness.invoker.invoke(
        dana_session,
        ToolCall(tool="erp.create_purchase_order", params=params, step_id="step-b"),
        grant=grant,
    )
    assert first.output["po_id"] != second.output["po_id"]


# --- the ledger ----------------------------------------------------------------


def test_the_audit_log_rejects_updates(harness, dana_session):
    import sqlite3

    dana_session.audit.emit(_SOME_EVENT, "an event to attempt to rewrite")

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        harness.store.execute("UPDATE audit_events SET summary = 'x' WHERE seq = 1")


def test_the_audit_log_rejects_deletes(harness, dana_session):
    import sqlite3

    dana_session.audit.emit(_SOME_EVENT, "an event to attempt to erase")

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        harness.store.execute("DELETE FROM audit_events WHERE seq = 1")


def test_the_hash_chain_detects_tampering(harness, dana_session):
    """Append-only enforced by convention is a promise; a chain is evidence.

    The triggers are dropped here to simulate an attacker with direct database
    access, which is the only threat model under which the chain is worth having.
    """
    harness.broker.gather(dana_session, ContextRequest(purpose="test"), systems=["mail"])
    assert harness.audit_log.verify_chain() == (True, None)

    harness.store.execute("DROP TRIGGER audit_events_no_update")
    harness.store.execute(
        "UPDATE audit_events SET summary = 'nothing to see here' WHERE seq = 2"
    )

    ok, broken = harness.audit_log.verify_chain()
    assert not ok
    assert broken is not None


def test_every_tool_effect_is_recorded(harness, dana_session, open_grant):
    harness.invoker.invoke(
        dana_session,
        ToolCall(
            tool="erp.create_purchase_order",
            params={
                "part_id": "P-4471",
                "supplier_id": "S-Z",
                "qty": 10,
                "need_by": "2026-09-07",
            },
            step_id="audit-1",
            rationale="because the test says so",
        ),
        grant=open_grant("erp.create_purchase_order"),
    )
    events = harness.audit_log.for_run("RUN-test")
    invoked = next(e for e in events if e.event_type.value == "tool.invoked")
    succeeded = next(e for e in events if e.event_type.value == "tool.succeeded")

    assert invoked.summary == "because the test says so"
    assert invoked.payload["params"]["supplier_id"] == "S-Z"
    assert succeeded.payload["output"]["po_id"]
