"""Detector registration and the context a detector runs in.

A detector is the only part of the harness that gets to decide, unprompted, that
something matters. Three constraints keep that power honest:

**It reads through the same scoped path as everything else.** A detector calls
:meth:`DetectorContext.scan`, which goes through the context broker, which enforces
scopes and records redactions. A detector cannot surface a problem the user is not
entitled to know about.

**It runs on a system principal, not as the user.** Detection happens on a schedule,
while the employee is asleep or out of office. The principal that does it holds
read-and-enqueue scopes and no writes at all.

**It returns findings, not conclusions.** An item carries structured ``facts`` that
code computed. Whether those facts warrant a reroute is the planner's judgment, and
keeping the two apart is what stops "the model thought there might be a delay" from
becoming an alert.

Detectors may be *targeted*: a scheduled follow-up invokes one with a payload
naming what to re-check. That is the whole mechanism behind the Tuesday arrival
check — a follow-up is not a special kind of work, it is a detector with an
argument.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from harmony.detect.models import AttentionItem
from harmony.identity.session import Session
from harmony.kernel.clock import Clock
from harmony.kernel.registry import Registry
from harmony.providers.base import ContextBundle, ContextRequest, SubjectRef
from harmony.providers.broker import ContextBroker


@dataclass
class DetectorContext:
    """What a detector is given: a scoped reader, a clock, and its arguments."""

    session: Session
    clock: Clock
    broker: ContextBroker
    systems: Sequence[str]
    payload: dict[str, Any] = field(default_factory=dict)
    """Arguments for a targeted invocation. Empty for a scheduled sweep."""

    def scan(
        self,
        *,
        purpose: str,
        subjects: Iterable[SubjectRef] = (),
        systems: Sequence[str] | None = None,
        **hints: Any,
    ) -> ContextBundle:
        """Read from the systems this detector declared.

        With no subjects, providers return their working set — bounded by hints
        such as ``horizon_days``. That is how a sweep and a targeted check share one
        interface rather than needing two.
        """
        request = ContextRequest(
            purpose=purpose,
            subjects=list(subjects),
            hints=hints,
        )
        return self.broker.gather(
            self.session, request, systems=list(systems or self.systems)
        )


@dataclass(frozen=True)
class DetectorSpec:
    """A registered detector."""

    id: str
    description: str
    systems: frozenset[str]
    required_scopes: frozenset[str]
    fn: Callable[[DetectorContext], Iterable[AttentionItem]]
    targeted: bool = False
    """True when this detector is normally invoked with a payload by a scheduled
    follow-up rather than swept on a timer."""

    def run(self, ctx: DetectorContext) -> list[AttentionItem]:
        return list(self.fn(ctx))


DETECTORS: Registry[DetectorSpec] = Registry("detector")


def detector(
    detector_id: str,
    *,
    description: str,
    systems: set[str] | frozenset[str],
    required_scopes: set[str] | frozenset[str],
    targeted: bool = False,
) -> Callable[
    [Callable[[DetectorContext], Iterable[AttentionItem]]],
    Callable[[DetectorContext], Iterable[AttentionItem]],
]:
    """Register a detector.

        @detector("material_shortfall",
                  description="Production orders whose parts will not arrive in time.",
                  systems={"erp", "mail"},
                  required_scopes={"erp:read", "mail:read"})
        def detect(ctx: DetectorContext) -> Iterable[AttentionItem]: ...

    Yield an :class:`AttentionItem` per finding, built with
    :meth:`AttentionItem.build` so its dedupe hashes are derived consistently.
    """

    def decorator(fn):
        DETECTORS.register(
            detector_id,
            DetectorSpec(
                id=detector_id,
                description=description,
                systems=frozenset(systems),
                required_scopes=frozenset(required_scopes),
                fn=fn,
                targeted=targeted,
            ),
        )
        return fn

    return decorator
