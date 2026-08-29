"""Attention items: the unit of "something a human should know about".

An item is what a detector produces and what the rest of the loop consumes. Its two
hashes are the interesting part, and they exist because "have I already told them
this?" is two questions wearing one coat:

``fingerprint``
    Identity of the *situation*: this part, this production order, this detector.
    Stable across re-runs. Dedupe keys on it.

``content_hash``
    Identity of the situation's *details*: how short, how late, by how much.
    Changes when the facts change.

Same fingerprint and same content is noise — suppress it. Same fingerprint, new
content is news — supersede the old item and run again. Collapsing these into one
hash gives you an agent that either nags or goes quiet after the first alert, and
both are wrong. Detectors never compute either hash; :meth:`AttentionItem.build`
derives them, so a detector author cannot get dedupe subtly wrong.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from harmony.kernel.ids import digest, new_id
from harmony.providers.base import SubjectRef


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return list(Severity).index(self)


class ItemState(StrEnum):
    OPEN = "open"
    """Raised and not yet resolved. A second identical detection is suppressed."""

    HANDLED = "handled"
    """A run reached a terminal state for this item."""

    SUPERSEDED = "superseded"
    """The situation changed materially; a newer item replaces this one."""

    SUPPRESSED = "suppressed"
    """A duplicate that was folded into an existing open item."""

    RESOLVED = "resolved"
    """The underlying condition no longer holds."""


class Evidence(BaseModel):
    """A pointer to the thing that justified the item.

    Evidence is not the data itself — it is a citation. The audit log holds the
    data; this says which part of it mattered. Keeping them separate stops an
    attention item from growing to hold a copy of the ERP.
    """

    source: str
    """System the fact came from: ``erp``, ``mail``, ``calendar``."""

    ref: str
    """Identifier within that system: ``PO-77812``, ``M-004``."""

    detail: str = ""
    """One line a human can read: "promised 09-04, supplier now says 09-08"."""

    quote: str | None = None
    """Verbatim source text, when the fact came from prose. This is what lets a
    reviewer check an extraction rather than trust it."""


class AttentionItem(BaseModel):
    """Something worth a person's attention, with the reasons attached."""

    item_id: str = Field(default_factory=lambda: new_id("AI"))
    detector_id: str
    principal_id: str
    title: str
    severity: Severity = Severity.MEDIUM
    subjects: list[SubjectRef] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    """The structured findings the planner reasons over. Everything here was
    computed by code, never asserted by a model."""

    fingerprint: str = ""
    content_hash: str = ""
    state: ItemState = ItemState.OPEN
    first_seen: _dt.datetime | None = None
    last_seen: _dt.datetime | None = None
    seen_count: int = 1
    run_id: str | None = None
    superseded_by: str | None = None

    @classmethod
    def build(
        cls,
        *,
        detector_id: str,
        principal_id: str,
        title: str,
        subjects: list[SubjectRef],
        facts: dict[str, Any],
        severity: Severity = Severity.MEDIUM,
        evidence: list[Evidence] | None = None,
        now: _dt.datetime | None = None,
    ) -> AttentionItem:
        """Construct an item with its hashes derived. The only supported way in."""
        item = cls(
            detector_id=detector_id,
            principal_id=principal_id,
            title=title,
            severity=severity,
            subjects=sorted(subjects, key=lambda s: (s.kind, s.id)),
            evidence=evidence or [],
            facts=facts,
            first_seen=now,
            last_seen=now,
        )
        item.fingerprint = digest(
            detector_id, principal_id, [str(s) for s in item.subjects]
        )
        item.content_hash = digest(facts)
        return item

    def describe(self) -> dict[str, Any]:
        """The shape handed to the planner."""
        # No item_id: the planner never refers to one, and a random identifier in
        # the prompt would make every recorded exchange unrepeatable.
        return {
            "detector": self.detector_id,
            "title": self.title,
            "severity": self.severity.value,
            "subjects": [{"kind": s.kind, "id": s.id} for s in self.subjects],
            "findings": self.facts,
            "evidence": [e.model_dump(exclude_none=True) for e in self.evidence],
        }
