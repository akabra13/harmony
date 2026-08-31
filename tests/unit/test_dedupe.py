"""Trigger dedupe: deciding whether a detection is news.

Named in the brief as a required test area. The interesting case is not "the same
thing twice is suppressed" — it is the pair: *unchanged* is suppressed, *changed*
supersedes. An implementation that keys only on identity gets the first right and
the second catastrophically wrong, by going quiet exactly when the situation
deteriorates.
"""

from __future__ import annotations

import pytest

from harmony.detect.dedupe import DedupeOutcome
from harmony.detect.models import AttentionItem, ItemState, Severity
from harmony.providers.base import SubjectRef


def _item(principal="u-101", *, shortfall=120, subjects=("part:P-4471",)) -> AttentionItem:
    return AttentionItem.build(
        detector_id="material_shortfall",
        principal_id=principal,
        title="P-4471 shortfall",
        subjects=[
            SubjectRef(kind=s.split(":")[0], id=s.split(":")[1]) for s in subjects
        ],
        facts={"shortfall_qty": shortfall},
        severity=Severity.HIGH,
    )


# --- the fingerprint -----------------------------------------------------------


def test_the_same_situation_has_the_same_fingerprint():
    assert _item().fingerprint == _item().fingerprint


def test_subject_order_does_not_change_the_fingerprint():
    """Detectors emit subjects in whatever order they happen to build them."""
    a = _item(subjects=("part:P-4471", "production_order:4812"))
    b = _item(subjects=("production_order:4812", "part:P-4471"))
    assert a.fingerprint == b.fingerprint


def test_a_different_subject_is_a_different_situation():
    assert _item(subjects=("part:P-4471",)).fingerprint != _item(
        subjects=("part:P-2218",)
    ).fingerprint


def test_a_different_person_is_a_different_situation():
    """Two people can be told about the same part independently."""
    assert _item("u-101").fingerprint != _item("u-303").fingerprint


def test_changed_details_change_the_content_hash_but_not_the_fingerprint():
    mild, severe = _item(shortfall=120), _item(shortfall=300)
    assert mild.fingerprint == severe.fingerprint
    assert mild.content_hash != severe.content_hash


# --- admission -----------------------------------------------------------------


def test_a_new_situation_is_raised(harness, dana_session):
    result = harness.items.admit(dana_session, _item())
    assert result.outcome is DedupeOutcome.RAISED
    assert result.should_run


def test_an_unchanged_repeat_is_suppressed(harness, dana_session):
    """Running a detector three times must not alert three times."""
    harness.items.admit(dana_session, _item())
    second = harness.items.admit(dana_session, _item())
    third = harness.items.admit(dana_session, _item())

    assert second.outcome is DedupeOutcome.SUPPRESSED
    assert third.outcome is DedupeOutcome.SUPPRESSED
    assert not second.should_run
    assert third.item.seen_count == 3


def test_a_changed_situation_supersedes_rather_than_suppressing(harness, dana_session):
    """The case a naive fingerprint check gets wrong.

    "There is a shortfall" is old news. "The shortfall doubled" is not, and an agent
    that stayed quiet through a deterioration would be worse than one that never
    alerted at all.
    """
    first = harness.items.admit(dana_session, _item(shortfall=120))
    second = harness.items.admit(dana_session, _item(shortfall=300))

    assert second.outcome is DedupeOutcome.SUPERSEDED
    assert second.should_run
    assert second.previous_item_id == first.item.item_id

    superseded = harness.items.get(first.item.item_id)
    assert superseded.state is ItemState.SUPERSEDED
    assert superseded.superseded_by == second.item.item_id


def test_a_resolved_situation_can_be_raised_again(harness, dana_session):
    """Dedupe suppresses against *open* items only. A problem that recurs after
    being dealt with is a new problem."""
    first = harness.items.admit(dana_session, _item())
    harness.items.set_state(first.item.item_id, ItemState.RESOLVED)

    assert harness.items.admit(dana_session, _item()).outcome is DedupeOutcome.RAISED


# --- through the detector ------------------------------------------------------


def test_running_the_detector_repeatedly_opens_one_run(harness, orchestrator):
    """End to end: three sweeps, one alert, one run."""
    first = orchestrator.detect("u-101")
    raised = [r for r in first if r.outcome is DedupeOutcome.RAISED]
    assert raised, "expected the first sweep to find something"

    for _ in range(2):
        again = orchestrator.detect("u-101")
        assert all(r.outcome is DedupeOutcome.SUPPRESSED for r in again)
        assert not any(r.should_run for r in again)


def test_suppression_is_audited(harness, orchestrator):
    """A suppressed detection is a decision, and decisions are recorded. Otherwise
    "why was I not told?" has no answer."""
    orchestrator.detect("u-101")
    orchestrator.detect("u-101")

    suppressions = [
        e
        for e in harness.audit_log.recent(200)
        if e.event_type.value == "detect.item_suppressed"
    ]
    assert suppressions
    assert all("fingerprint" in e.payload for e in suppressions)


def test_supersession_records_both_sets_of_findings(harness, dana_session):
    """So a reader can see what changed, not merely that something did."""
    harness.items.admit(dana_session, _item(shortfall=120))
    harness.items.admit(dana_session, _item(shortfall=300))

    event = next(
        e
        for e in harness.audit_log.recent(50)
        if e.event_type.value == "detect.item_superseded"
    )
    assert event.payload["previous_findings"]["shortfall_qty"] == 120
    assert event.payload["findings"]["shortfall_qty"] == 300


def test_an_item_raised_without_a_run_is_picked_up_next_time(harness, orchestrator):
    """Stranded items must not suppress their own follow-up.

    Detection and planning can come apart — a sweep run on its own, or a process
    that dies between raising an item and opening a run for it. The item stays
    open, and every later detection of the same situation is then suppressed
    against it. The failure mode is the worst available: the agent goes quiet about
    a problem *because* it already noticed it.
    """
    raised = [r for r in orchestrator.detect("u-101") if r.should_run]
    assert raised, "expected the sweep to find something"
    assert all(r.item.run_id is None for r in raised), "no run attached yet"

    runs = orchestrator.detect_and_run("u-101")

    assert len(runs) == len(raised), "the stranded items should each get a run"
    for result in raised:
        assert harness.items.get(result.item.item_id).run_id is not None


def test_an_item_that_already_has_a_run_is_not_run_again(harness, orchestrator):
    """The other half: picking up strays must not mean re-running everything."""
    orchestrator.detect_and_run("u-101")
    before = len(harness.runs.recent(50))

    orchestrator.detect_and_run("u-101")

    assert len(harness.runs.recent(50)) == before, "a second sweep opened duplicate runs"


def test_a_targeted_follow_up_does_not_sweep_up_unrelated_items(harness, orchestrator):
    """A follow-up answers one question.

    Picking up strays on the way would attribute unrelated items to that follow-up
    — wrong trigger, wrong parent run, and an audit trail claiming a purchase-order
    arrival check opened a run about a different part entirely.
    """
    from harmony.runtime.run import TriggerKind

    stranded = [r.item for r in orchestrator.detect("u-101") if r.should_run]
    assert stranded, "expected the sweep to leave items with no run"

    runs = orchestrator.detect_and_run(
        "u-101",
        detector_ids=["po_arrival_check"],
        payload={"po_id": "PO-77812", "production_order_id": "4812"},
        trigger=TriggerKind.FOLLOW_UP,
    )

    assert all(r.attention_item_id not in {i.item_id for i in stranded} for r in runs)
