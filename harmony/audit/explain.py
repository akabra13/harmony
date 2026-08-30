"""Reconstructing a run from the audit log alone.

Requirement 7 of the brief: *"from the audit log alone, someone should be able to
reconstruct what the agent saw, what it concluded, what it was allowed to do, who
approved what, and what actually happened in each system."*

This renderer reads **only** ``audit_events``. It does not consult the runs table,
the proposals table, or the systems of record, and it is written that way on
purpose: if the narrative can be produced without them, the requirement is met, and
if a field is missing from the story then it was missing from the ledger, which is
a bug worth finding.

The output is organised as the five questions the requirement asks, because a
chronological event dump is not a reconstruction — it is raw material for one.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from typing import Any

from harmony.audit.log import AuditLog
from harmony.audit.models import AuditEvent, EventType

SECTIONS: list[tuple[str, str, set[EventType]]] = [
    (
        "What the agent saw",
        "Detection, the context it gathered, and what it was not permitted to read.",
        {
            EventType.DETECTOR_RAN,
            EventType.ATTENTION_ITEM_RAISED,
            EventType.ATTENTION_ITEM_SUPPRESSED,
            EventType.ATTENTION_ITEM_SUPERSEDED,
            EventType.CONTEXT_REQUESTED,
            EventType.CONTEXT_SLICE_FETCHED,
            EventType.CONTEXT_PROVIDER_SKIPPED,
            EventType.CONTEXT_REDACTED,
            EventType.MEMORY_RECALLED,
        },
    ),
    (
        "What the agent concluded",
        "Every model call, what it was allowed to answer, and the resulting proposal.",
        {
            EventType.LLM_CALLED,
            EventType.LLM_OUTPUT_REJECTED,
            EventType.PLAN_PROPOSED,
            EventType.PLAN_REJECTED,
        },
    ),
    (
        "What the agent was allowed to do",
        "Each policy rule, its verdict, and the inputs it used.",
        {
            EventType.GATE_RULE_EVALUATED,
            EventType.GATE_EVALUATED,
            EventType.SCOPE_DENIED,
        },
    ),
    (
        "Who approved what",
        "The request, any escalation, and the decision.",
        {
            EventType.APPROVAL_REQUESTED,
            EventType.APPROVAL_GRANTED,
            EventType.APPROVAL_REJECTED,
            EventType.APPROVAL_ESCALATED,
            EventType.APPROVAL_EXPIRED,
        },
    ),
    (
        "What actually happened in each system",
        "Every effect, every rollback, and every piece of work deferred to later.",
        {
            EventType.WORKFLOW_STARTED,
            EventType.WORKFLOW_STEP_STARTED,
            EventType.WORKFLOW_STEP_COMPLETED,
            EventType.WORKFLOW_STEP_FAILED,
            EventType.WORKFLOW_RESUMED,
            EventType.WORKFLOW_COMPLETED,
            EventType.WORKFLOW_COMPENSATING,
            EventType.WORKFLOW_COMPENSATION_STEP,
            EventType.WORKFLOW_COMPENSATED,
            EventType.WORKFLOW_COMPENSATION_FAILED,
            EventType.TOOL_INVOKED,
            EventType.TOOL_SUCCEEDED,
            EventType.TOOL_FAILED,
            EventType.TOOL_REPLAYED,
            EventType.SCHEDULE_TASK_CREATED,
            EventType.SCHEDULE_TASK_FIRED,
            EventType.SCHEDULE_TASK_FAILED,
            EventType.MEMORY_PROMOTED,
            EventType.MEMORY_DEMOTED,
        },
    ),
]


class RunExplainer:
    """Turns a run's audit events into a narrative a person can read."""

    def __init__(self, log: AuditLog) -> None:
        self._log = log

    def explain(self, run_id: str, *, verbose: bool = False) -> str:
        events = self._log.for_run(run_id)
        if not events:
            return f"No audit events recorded for run '{run_id}'."
        return "\n".join(self._render(run_id, events, verbose=verbose))

    def explain_markdown(self, run_id: str, *, verbose: bool = False) -> str:
        events = self._log.for_run(run_id)
        if not events:
            return f"No audit events recorded for run `{run_id}`."
        return "\n".join(self._render_markdown(run_id, events, verbose=verbose))

    # --- plain text ------------------------------------------------------------

    def _render(self, run_id: str, events: Sequence[AuditEvent], *, verbose: bool) -> list[str]:
        lines = [
            "=" * 78,
            f"RUN {run_id}",
            "=" * 78,
            *self._headline(events),
            "",
        ]
        for title, subtitle, types in SECTIONS:
            matching = [e for e in events if e.event_type in types]
            if not matching:
                continue
            lines += [f"── {title} ".ljust(78, "─"), f"   {subtitle}", ""]
            for event in matching:
                lines += self._event_lines(event, verbose=verbose, indent="   ")
            lines.append("")

        lines += self._outcome(events)
        lines += ["", self._integrity_line()]
        return lines

    def _event_lines(self, event: AuditEvent, *, verbose: bool, indent: str) -> list[str]:
        stamp = event.ts_clock.strftime("%Y-%m-%d %H:%M")
        lines = [f"{indent}[{stamp}] {event.actor_id:<16} {event.summary}"]
        for key, value in self._salient(event, verbose=verbose).items():
            lines.append(f"{indent}    · {key}: {_short(value)}")
        return lines

    # --- markdown --------------------------------------------------------------

    def _render_markdown(
        self, run_id: str, events: Sequence[AuditEvent], *, verbose: bool
    ) -> list[str]:
        lines = [f"# Run `{run_id}`", ""]
        lines += [f"> {line}" for line in self._headline(events, include_wall=False)]
        lines.append("")

        for title, subtitle, types in SECTIONS:
            matching = [e for e in events if e.event_type in types]
            if not matching:
                continue
            lines += [f"## {title}", "", f"*{subtitle}*", ""]
            for event in matching:
                stamp = event.ts_clock.strftime("%Y-%m-%d %H:%M")
                lines.append(f"- **{stamp}** · `{event.actor_id}` — {event.summary}")
                for key, value in self._salient(event, verbose=verbose).items():
                    lines.append(f"  - `{key}`: {_short(value)}")
            lines.append("")

        lines += ["## Outcome", ""] + [f"- {line}" for line in self._outcome(events)]
        lines += ["", f"*{self._integrity_line()}*"]
        return lines

    # --- summary pieces --------------------------------------------------------

    @staticmethod
    def _headline(events: Sequence[AuditEvent], *, include_wall: bool = True) -> list[str]:
        """The run in four lines.

        ``include_wall`` is false for markdown. Real elapsed time is an operational
        fact worth seeing in a terminal — it distinguishes "the agent waited five
        days" from "the demo ran in a fifth of a second" — but it varies between
        identical runs, and a committed artifact that changes on every regeneration
        is one nobody can diff. The markdown records simulated time, which is the
        business fact and is reproducible.
        """
        start = next((e for e in events if e.event_type is EventType.RUN_STARTED), events[0])
        span_clock = f"{events[0].ts_clock:%Y-%m-%d %H:%M} → {events[-1].ts_clock:%Y-%m-%d %H:%M}"
        wall = (events[-1].ts_wall - events[0].ts_wall).total_seconds()
        return [
            start.summary,
            f"acting for: {start.actor_id}   profile: {start.payload.get('profile', '—')}",
            f"simulated time: {span_clock}"
            + (f"   (real elapsed: {wall:.1f}s)" if include_wall else ""),
            f"events: {len(events)}",
        ]

    @staticmethod
    def _outcome(events: Sequence[AuditEvent]) -> list[str]:
        transitions = [e for e in events if e.event_type is EventType.RUN_STATE_CHANGED]
        if not transitions:
            return ["No state transitions recorded."]
        path = " → ".join(
            [transitions[0].payload.get("from_state", "?")]
            + [t.payload.get("to_state", "?") for t in transitions]
        )
        return [f"state path: {path}", f"final state: {transitions[-1].payload.get('to_state')}"]

    def _integrity_line(self) -> str:
        ok, broken = self._log.verify_chain()
        return (
            "Audit chain verified: every entry hashes its predecessor."
            if ok
            else f"AUDIT CHAIN BROKEN at event {broken} — this ledger has been tampered with."
        )

    @staticmethod
    def _salient(event: AuditEvent, *, verbose: bool) -> dict[str, Any]:
        """The payload fields worth showing inline.

        Curated rather than exhaustive: a narrative that printed every field would
        be as unreadable as the raw table, and the point of this renderer is that a
        person can follow it. ``verbose`` shows everything for when they cannot.
        """
        payload = event.payload
        if verbose:
            return {k: v for k, v in payload.items() if v not in (None, [], {}, "")}

        keys_by_type: dict[EventType, tuple[str, ...]] = {
            EventType.ATTENTION_ITEM_RAISED: ("detector", "severity", "subjects"),
            EventType.CONTEXT_SLICE_FETCHED: ("counts",),
            EventType.CONTEXT_REDACTED: ("collection", "count", "reason"),
            EventType.CONTEXT_PROVIDER_SKIPPED: ("system", "missing"),
            EventType.LLM_CALLED: ("call_site", "model", "input_tokens", "output_tokens"),
            EventType.LLM_OUTPUT_REJECTED: ("call_site", "error"),
            EventType.PLAN_PROPOSED: ("action_kind", "digest"),
            EventType.PLAN_REJECTED: ("named", "unknown"),
            EventType.GATE_RULE_EVALUATED: ("verdict", "approver_id"),
            EventType.GATE_EVALUATED: ("verdict", "approver_id", "denials"),
            EventType.SCOPE_DENIED: ("missing", "subject", "reason"),
            EventType.APPROVAL_REQUESTED: ("approval_id", "approver_id"),
            EventType.APPROVAL_ESCALATED: ("from_approver", "to_approver", "checked_day"),
            EventType.APPROVAL_GRANTED: ("approval_id", "decided_by"),
            EventType.TOOL_INVOKED: ("tool", "writes", "params"),
            EventType.TOOL_SUCCEEDED: ("tool", "output"),
            EventType.TOOL_FAILED: ("tool", "error"),
            EventType.TOOL_REPLAYED: ("tool", "idempotency_key"),
            EventType.WORKFLOW_STARTED: ("workflow", "version", "params"),
            EventType.WORKFLOW_STEP_COMPLETED: ("step_id", "output"),
            EventType.WORKFLOW_COMPENSATION_STEP: ("step_id", "outcome"),
            EventType.SCHEDULE_TASK_CREATED: ("kind", "fire_at"),
        }
        keys = keys_by_type.get(event.event_type, ())
        return {k: payload[k] for k in keys if payload.get(k) not in (None, [], {}, "")}


def _short(value: Any, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
