"""Workflow resumption and compensation.

The third area the brief names. Two properties matter, and they are the two that a
killed process makes visible:

**Resume lands on the right step.** Progress is a persisted cursor, advanced in the
same transaction that records a step's result, so there is no window in which a step
is done but unrecorded.

**Resume does not duplicate effects.** The resumed process may legitimately
re-attempt the step it died inside. The idempotency key makes that a replay rather
than a second purchase order — and a test that only checked the cursor would miss
the case that actually costs money.

The kill is simulated by abandoning the engine mid-run rather than by sending a
signal: the failure suite does the real ``kill -9``, and here the point is to assert
precisely which effects exist afterwards.
"""

from __future__ import annotations

import pytest

from harmony.kernel.errors import ToolFailed
from harmony.tools.base import ToolCall
from harmony.workflow.models import InstanceStatus
from northfield.systems import erp

PARAMS = {
    "at_risk_po_id": "PO-77812",
    "part_id": "P-4471",
    "production_order_id": "4812",
    "required_on_site_by": "2026-09-07",
    "qty": 400,
    "supervisor_id": "u-301",
}


@pytest.fixture
def reroute(harness):
    return harness.workflows.get("po_reroute", 3)


@pytest.fixture
def grant(harness, reroute, open_grant):
    return open_grant(*reroute.tool_names())


def _replacements(harness):
    return [
        po
        for po in erp.list_purchase_orders(harness.store, part_id="P-4471")
        if po["replaces_po"] == "PO-77812"
    ]


# --- ordinary completion -------------------------------------------------------


def test_the_workflow_runs_every_step_in_order(harness, dana_session, reroute, grant):
    instance = harness.engine.start(
        dana_session, definition=reroute, params=PARAMS, grant=grant
    )

    assert instance.status is InstanceStatus.COMPLETED
    assert instance.cursor == len(reroute.steps)
    assert list(instance.step_results) == [s.id for s in reroute.steps]


def test_the_model_cannot_choose_outside_the_filtered_candidates(
    harness, dana_session, reroute, grant
):
    """Halstead is qualified and cheaper; a nine-day lead time removes it before the
    model ever sees it. Meridian is the only survivor."""
    instance = harness.engine.start(
        dana_session, definition=reroute, params=PARAMS, grant=grant
    )
    candidates = instance.step_results["filter_by_lead_time"]["output"]["supplier_ids"]
    chosen = instance.step_results["choose_supplier"]["output"]["supplier_id"]

    assert candidates == ["S-Z"]
    assert chosen in candidates


# --- resumption ----------------------------------------------------------------


class ProcessKilled(BaseException):
    """Stands in for the process dying.

    Deliberately a ``BaseException``. A step raising a normal exception is a *step
    failure*, which the engine handles by rolling back — correct, and not what
    resumption is for. A killed process runs no cleanup at all, and inheriting from
    ``BaseException`` is what makes the engine's ``except Exception`` let it through
    untouched, leaving exactly the state a ``kill -9`` would.
    """


def test_resume_continues_from_the_persisted_cursor(
    harness, dana_session, reroute, grant, monkeypatch
):
    """Die during step 5, reload from the database, and carry on."""
    real_invoke = harness.invoker.invoke

    def die_at_step_five(session, call: ToolCall, **kwargs):
        if call.tool == "erp.cancel_or_reduce_purchase_order":
            raise ProcessKilled("the process died here")
        return real_invoke(session, call, **kwargs)

    monkeypatch.setattr(harness.invoker, "invoke", die_at_step_five)
    with pytest.raises(ProcessKilled):
        harness.engine.start(dana_session, definition=reroute, params=PARAMS, grant=grant)

    instance = harness.engine._repo.for_run(dana_session.run_id)[0]
    assert instance.cursor == 4  # four steps committed
    assert "create_replacement_po" in instance.step_results
    assert "reduce_original_po" not in instance.step_results

    # A restarted process loads the instance from the database and continues.
    # Restoring the real invoker stands in for the new process.
    monkeypatch.setattr(harness.invoker, "invoke", real_invoke)
    resumed = harness.engine.resume(dana_session, instance.instance_id, grant=grant)

    assert resumed.status is InstanceStatus.COMPLETED
    assert list(resumed.step_results) == [s.id for s in reroute.steps]


def test_resume_does_not_create_a_second_purchase_order(
    harness, dana_session, reroute, grant, monkeypatch
):
    """The property that actually costs money if it fails."""
    real_invoke = harness.invoker.invoke

    def die(session, call: ToolCall, **kwargs):
        if call.tool == "erp.cancel_or_reduce_purchase_order":
            raise ProcessKilled("killed mid-workflow")
        return real_invoke(session, call, **kwargs)

    monkeypatch.setattr(harness.invoker, "invoke", die)
    with pytest.raises(ProcessKilled):
        harness.engine.start(dana_session, definition=reroute, params=PARAMS, grant=grant)

    assert len(_replacements(harness)) == 1
    created = _replacements(harness)[0]["po_id"]

    monkeypatch.setattr(harness.invoker, "invoke", real_invoke)
    instance = harness.engine._repo.for_run(dana_session.run_id)[0]
    harness.engine.resume(dana_session, instance.instance_id, grant=grant)

    replacements = _replacements(harness)
    assert len(replacements) == 1, "resume must not raise a second purchase order"
    assert replacements[0]["po_id"] == created


def test_a_replayed_step_is_audited_as_a_replay(harness, dana_session, reroute, grant):
    """An auditor must be able to tell "we did it" from "we had already done it"."""
    instance = harness.engine.start(
        dana_session, definition=reroute, params=PARAMS, grant=grant
    )
    # Re-run the create step with the same run and step id: the idempotency key is
    # identical, so this is the same logical write.
    result = harness.invoker.invoke(
        dana_session,
        ToolCall(
            tool="erp.create_purchase_order",
            params={
                "part_id": "P-4471",
                "supplier_id": "S-Z",
                "qty": 400,
                "need_by": "2026-09-07",
                "replaces_po": "PO-77812",
                "reason": instance.step_results["choose_supplier"]["output"]["justification"],
            },
            step_id=f"{instance.instance_id}:create_replacement_po",
        ),
        grant=grant,
    )

    assert result.replayed is True
    assert len(_replacements(harness)) == 1
    assert any(
        e.event_type.value == "tool.replayed"
        for e in harness.audit_log.for_run(dana_session.run_id)
    )


# --- compensation --------------------------------------------------------------


def test_a_late_failure_rolls_back_earlier_writes(
    harness, dana_session, reroute, grant, monkeypatch
):
    """Fail at step 5; step 4's purchase order must be cancelled."""
    real_invoke = harness.invoker.invoke

    def fail_at_reduce(session, call: ToolCall, **kwargs):
        if call.tool == "erp.cancel_or_reduce_purchase_order" and "compensate" not in call.step_id:
            raise ToolFailed("ERP rejected the change")
        return real_invoke(session, call, **kwargs)

    monkeypatch.setattr(harness.invoker, "invoke", fail_at_reduce)
    instance = harness.engine.start(
        dana_session, definition=reroute, params=PARAMS, grant=grant
    )

    assert instance.status is InstanceStatus.COMPENSATED
    replacements = _replacements(harness)
    assert replacements and replacements[0]["status"] == "cancelled"

    original = erp.get_purchase_order(harness.store, "PO-77812")
    assert original["status"] == "open", "the original must be left untouched"


def test_compensation_runs_in_reverse_order(
    harness, dana_session, reroute, grant, monkeypatch
):
    """Later steps were built on earlier ones, so undoing forwards would briefly
    produce a state that never legitimately occurred."""
    real_invoke = harness.invoker.invoke

    def fail_at_notify(session, call: ToolCall, **kwargs):
        if call.tool == "production.notify_supervisor":
            raise ToolFailed("mail gateway down")
        return real_invoke(session, call, **kwargs)

    monkeypatch.setattr(harness.invoker, "invoke", fail_at_notify)
    instance = harness.engine.start(
        dana_session, definition=reroute, params=PARAMS, grant=grant
    )

    compensated = [e["step_id"] for e in instance.compensation_log]
    # Every completed step, newest first. Read-only and model steps have nothing to
    # undo and say so, rather than being skipped silently.
    assert compensated == [
        "draft_notification",
        "reduce_original_po",
        "create_replacement_po",
        "choose_supplier",
        "filter_by_lead_time",
        "find_approved_suppliers",
    ]

    original = erp.get_purchase_order(harness.store, "PO-77812")
    assert original["status"] == "open"
    assert original["qty"] == 400


def test_an_irreversible_step_is_recorded_as_such_not_hidden(
    harness, dana_session, reroute, grant, monkeypatch
):
    """A rollback that pretends a sent message was unsent makes the ledger lie."""
    real_invoke = harness.invoker.invoke

    def fail_at_schedule(session, call: ToolCall, **kwargs):
        if call.tool == "schedule.create_followup":
            raise ToolFailed("queue unavailable")
        return real_invoke(session, call, **kwargs)

    monkeypatch.setattr(harness.invoker, "invoke", fail_at_schedule)
    instance = harness.engine.start(
        dana_session, definition=reroute, params=PARAMS, grant=grant
    )

    notify = next(e for e in instance.compensation_log if e["step_id"] == "notify_production")
    assert notify["outcome"] == "irreversible"
    assert instance.status is InstanceStatus.COMPENSATED


# --- preconditions -------------------------------------------------------------


def test_no_qualified_supplier_stops_the_workflow(harness, dana_session, reroute, grant):
    """``on_empty: fail`` is what makes step 1 a confirmation rather than a lookup."""
    harness.store.execute(
        "UPDATE suppliers SET approved_parts = '[]' WHERE supplier_id IN ('S-Y','S-Z','S-W')"
    )
    instance = harness.engine.start(
        dana_session,
        definition=reroute,
        params={**PARAMS, "part_id": "P-4471"},
        grant=grant,
    )

    assert instance.status is InstanceStatus.COMPENSATED
    assert _replacements(harness) == []
    assert "create_replacement_po" not in instance.step_results
