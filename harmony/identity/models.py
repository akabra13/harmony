"""Who is acting, and what they are entitled to do.

Scopes are opaque strings to the kernel. ``erp:po:create`` means nothing here; it
is matched, intersected and compared, never interpreted. That is what lets the same
harness serve a manufacturer and a hospital without change.

The one piece of structure the kernel does impose is the ``system:object:verb``
convention, used only by :meth:`ScopeSet.for_system` to answer "which of this
principal's scopes belong to the mail system?" — a question the context broker asks
when deciding whether a provider is reachable at all.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class PrincipalKind(StrEnum):
    """Who the harness is acting as."""

    USER = "user"
    """A human employee. Every write in the system traces back to one of these."""

    SYSTEM = "system"
    """A narrow non-human identity. Detectors run as this while the user is asleep,
    and it is deliberately incapable of writing — see policy.yaml."""


class ScopeSet(frozenset[str]):
    """A set of scope strings with a few domain-free conveniences."""

    def covers(self, required: frozenset[str] | set[str]) -> bool:
        return set(required).issubset(self)

    def missing(self, required: frozenset[str] | set[str]) -> frozenset[str]:
        return frozenset(set(required) - self)

    def for_system(self, system: str) -> frozenset[str]:
        """Scopes whose first segment is ``system``."""
        return frozenset(s for s in self if s.split(":", 1)[0] == system)

    def intersect(self, other: frozenset[str] | set[str]) -> ScopeSet:
        return ScopeSet(self & set(other))


class ApprovalLimits(BaseModel):
    """Per-user ceilings above which a decision escalates to someone senior.

    Keys are policy-defined (``po_create_max_value``); the kernel only looks them up
    by name on behalf of a gate rule that names the key it cares about.
    """

    model_config = {"extra": "allow"}

    def limit_for(self, key: str) -> float | None:
        value = getattr(self, key, None)
        if value is None and hasattr(self, "model_extra"):
            value = (self.model_extra or {}).get(key)
        return float(value) if value is not None else None


class Principal(BaseModel):
    """The acting identity for a run.

    In this build a Principal is resolved from seed data. In a real deployment it
    is minted from an SSO assertion and its scopes come from a token exchange —
    the shape stays the same, which is the point of holding it as one object.
    """

    id: str
    kind: PrincipalKind = PrincipalKind.USER
    name: str = ""
    email: str = ""
    role: str = ""
    manager_id: str | None = None
    backup_approver_id: str | None = None
    scopes: ScopeSet = Field(default_factory=lambda: ScopeSet())
    approval_limits: ApprovalLimits = Field(default_factory=ApprovalLimits)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})" if self.name else self.id

    def can(self, *scopes: str) -> bool:
        return self.scopes.covers(frozenset(scopes))
