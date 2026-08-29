"""Context providers: one per system, each answerable for what it did and did not show.

A provider turns "here is the situation we care about" into "here is what this user
is allowed to see about it, from my system". Two properties make the abstraction
carry its weight:

**It is domain-free.** A request names subjects as ``(kind, id)`` pairs — the kernel
never learns that ``part`` and ``production_order`` are different sorts of thing.
Providers interpret the kinds they recognise and ignore the rest, so adding a new
kind of subject does not touch the kernel or any existing provider.

**It reports what it withheld.** A slice carries :class:`Redaction` records
alongside its data. This is the difference between an audit trail that shows what
the agent read and one that shows what the agent *could* read — and only the second
one lets a reviewer tell "the agent ignored the warning email" apart from "the agent
was never permitted to see it".
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from harmony.kernel.registry import Registry

if TYPE_CHECKING:
    from harmony.identity.session import Session


class SubjectRef(BaseModel):
    """A pointer to something the run is about.

    ``kind`` is an opaque string to the harness. Detectors emit them, providers
    recognise the ones they serve, and the audit renders them verbatim.
    """

    kind: str
    id: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"

    def __hash__(self) -> int:
        return hash((self.kind, self.id))


class ContextRequest(BaseModel):
    """What the run wants to know about."""

    purpose: str
    subjects: list[SubjectRef] = Field(default_factory=list)
    since: _dt.date | None = None
    hints: dict[str, Any] = Field(default_factory=dict)

    def subjects_of(self, kind: str) -> list[str]:
        """Ids of subjects of one kind. The main thing providers call."""
        return [s.id for s in self.subjects if s.kind == kind]


class Redaction(BaseModel):
    """A record of data that existed but was not returned."""

    collection: str
    count: int
    reason: str


class ContextSlice(BaseModel):
    """One system's answer, plus an account of what it held back."""

    system: str
    provider: str
    collections: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    redactions: list[Redaction] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def record_count(self) -> int:
        return sum(len(rows) for rows in self.collections.values())

    def redacted_count(self) -> int:
        return sum(r.count for r in self.redactions)

    def get(self, collection: str) -> list[dict[str, Any]]:
        return self.collections.get(collection, [])


class ContextBundle(BaseModel):
    """Everything gathered for one run, across every reachable system."""

    slices: list[ContextSlice] = Field(default_factory=list)
    unreachable: list[dict[str, Any]] = Field(default_factory=list)
    """Providers the profile declared but the session could not reach, with the
    scopes that were missing. Present in the bundle *and* in the prompt: the model
    is told what it cannot see, so it can say "I could not check the calendar"
    instead of quietly assuming there was nothing there."""

    def system(self, name: str) -> ContextSlice | None:
        for slice_ in self.slices:
            if slice_.system == name:
                return slice_
        return None

    def records(self, system: str, collection: str) -> list[dict[str, Any]]:
        slice_ = self.system(system)
        return slice_.get(collection) if slice_ else []

    def one(self, system: str, collection: str, **match: Any) -> dict[str, Any] | None:
        for row in self.records(system, collection):
            if all(row.get(k) == v for k, v in match.items()):
                return row
        return None

    def summarise(self) -> dict[str, Any]:
        """A compact shape for audit payloads — counts, not contents."""
        return {
            "systems": {
                s.system: {
                    "collections": {k: len(v) for k, v in s.collections.items()},
                    "redacted": s.redacted_count(),
                }
                for s in self.slices
            },
            "unreachable": self.unreachable,
        }


@runtime_checkable
class ContextProvider(Protocol):
    """The interface every system connector satisfies."""

    system: str
    required_scopes: frozenset[str]

    def fetch(self, session: "Session", request: ContextRequest) -> ContextSlice: ...


@dataclass(frozen=True)
class ProviderSpec:
    """A registered provider."""

    system: str
    description: str
    required_scopes: frozenset[str]
    fn: Callable[["Session", ContextRequest], ContextSlice]

    def fetch(self, session: "Session", request: ContextRequest) -> ContextSlice:
        return self.fn(session, request)


PROVIDERS: Registry[ProviderSpec] = Registry("provider")


def provider(
    system: str,
    *,
    description: str,
    required_scopes: set[str] | frozenset[str],
) -> Callable[
    [Callable[["Session", ContextRequest], ContextSlice]],
    Callable[["Session", ContextRequest], ContextSlice],
]:
    """Register a context provider for one system.

        @provider("calendar",
                  description="Meetings and out-of-office periods.",
                  required_scopes={"calendar:read"})
        def calendar_provider(session, request) -> ContextSlice: ...

    The function may assume the session holds ``required_scopes``; the broker
    checks before calling, and re-checking here would let the two drift.
    """

    def decorator(fn):
        PROVIDERS.register(
            system,
            ProviderSpec(
                system=system,
                description=description,
                required_scopes=frozenset(required_scopes),
                fn=fn,
            ),
        )
        return fn

    return decorator
