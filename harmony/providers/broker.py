"""The context broker: fan out to providers, enforce scope, record the blind spots.

This is the read-side chokepoint, mirroring the tool invoker on the write side.
Providers are never called directly by planners or detectors; they are called
through here, which means the scope check and the audit record happen once, in a
place that cannot be forgotten.

The broker treats an unreachable provider as *information*, not as an error. A
purchasing manager whose profile lists a calendar provider they lack scope for gets
a bundle that says so, and the planner is told. Silently returning an empty result
would let the agent conclude "nothing on the calendar" from "I was not allowed to
look" — a failure mode that is invisible in exactly the situations where it matters.
"""

from __future__ import annotations

from collections.abc import Sequence

from harmony.audit.models import EventType
from harmony.identity.session import Session
from harmony.providers.base import PROVIDERS, ContextBundle, ContextRequest, ContextSlice


class ContextBroker:
    """Gathers context across the systems a profile declares."""

    def __init__(self, registry=PROVIDERS) -> None:
        self._registry = registry

    def gather(
        self,
        session: Session,
        request: ContextRequest,
        *,
        systems: Sequence[str],
    ) -> ContextBundle:
        """Fetch from each named system the session can reach."""
        session.audit.emit(
            EventType.CONTEXT_REQUESTED,
            f"gathering context for {request.purpose}",
            systems=list(systems),
            subjects=[str(s) for s in request.subjects],
            purpose=request.purpose,
        )

        bundle = ContextBundle()
        for system in systems:
            spec = self._registry.try_get(system)
            if spec is None:
                bundle.unreachable.append(
                    {"system": system, "reason": "no provider registered"}
                )
                session.audit.emit(
                    EventType.CONTEXT_PROVIDER_SKIPPED,
                    f"no provider registered for '{system}'",
                    system=system,
                    reason="not_registered",
                )
                continue

            missing = session.scopes.missing(spec.required_scopes)
            if missing:
                bundle.unreachable.append(
                    {
                        "system": system,
                        "reason": "insufficient scope",
                        "missing_scopes": sorted(missing),
                    }
                )
                session.audit.emit(
                    EventType.CONTEXT_PROVIDER_SKIPPED,
                    f"{session.principal.label} may not read '{system}'",
                    system=system,
                    reason="insufficient_scope",
                    required=sorted(spec.required_scopes),
                    missing=sorted(missing),
                )
                continue

            slice_ = spec.fetch(session, request)
            bundle.slices.append(slice_)
            self._audit_slice(session, slice_)

        return bundle

    @staticmethod
    def _audit_slice(session: Session, slice_: ContextSlice) -> None:
        session.audit.emit(
            EventType.CONTEXT_SLICE_FETCHED,
            f"read {slice_.record_count()} record(s) from {slice_.system}",
            system=slice_.system,
            provider=slice_.provider,
            counts={name: len(rows) for name, rows in slice_.collections.items()},
            notes=slice_.notes,
        )
        for redaction in slice_.redactions:
            session.audit.emit(
                EventType.CONTEXT_REDACTED,
                f"withheld {redaction.count} {redaction.collection} record(s) "
                f"from {slice_.system}: {redaction.reason}",
                system=slice_.system,
                collection=redaction.collection,
                count=redaction.count,
                reason=redaction.reason,
            )
