"""The scoped session: the harness's stand-in for a downscoped access token.

Nothing in the harness reads or writes without one of these. That is the whole
point: scope enforcement then lives in exactly two chokepoints — the context broker
(reads) and the tool invoker (writes) — instead of being re-implemented, and
eventually forgotten, in every provider and tool.

**The effective scope set is an intersection**, computed in :meth:`Session.issue`:

    user's entitlements  ∩  the profile's declared needs  ∩  this run's purpose

A user entitled to ``erp:po:cancel`` who is running a profile that never declared
it simply does not have it for this run. Least privilege by construction, and a
compromised prompt cannot widen the set — there is no code path that adds a scope
to a live session. In a real deployment the same object holds references to
per-system on-behalf-of tokens; see DESIGN.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from harmony.audit.log import AuditWriter
from harmony.audit.models import EventType
from harmony.identity.models import Principal, PrincipalKind, ScopeSet
from harmony.kernel.clock import Clock
from harmony.kernel.errors import ScopeDenied
from harmony.kernel.ids import short_digest
from harmony.kernel.services import RuntimeServices


@dataclass(frozen=True)
class Session:
    """An authorised, audited, time-aware context for one run."""

    principal: Principal
    scopes: ScopeSet
    run_id: str
    audit: AuditWriter
    clock: Clock
    purpose: str = ""
    granted_by: tuple[str, ...] = field(default=())
    """Provenance of the scope set: which constraints produced it. Rendered in the
    audit so a reader can see *why* the agent could not see something."""

    services: RuntimeServices | None = None
    """Infrastructure handles for tools and providers. See
    :mod:`harmony.kernel.services` for why they live here."""

    call_key: str = ""
    """The idempotency key of the tool call currently executing. Set by the invoker
    for the duration of one invocation; empty outside one. See :meth:`derive_id`."""

    # --- construction ----------------------------------------------------------

    @classmethod
    def issue(
        cls,
        *,
        principal: Principal,
        run_id: str,
        audit: AuditWriter,
        clock: Clock,
        profile_scopes: frozenset[str] | None = None,
        purpose_scopes: frozenset[str] | None = None,
        purpose: str = "",
        services: RuntimeServices | None = None,
    ) -> Session:
        """Mint a session whose scopes are the intersection of every constraint."""
        effective = ScopeSet(principal.scopes)
        granted_by = ["principal"]
        if profile_scopes is not None:
            effective = effective.intersect(profile_scopes)
            granted_by.append("profile")
        if purpose_scopes is not None:
            effective = effective.intersect(purpose_scopes)
            granted_by.append("purpose")
        return cls(
            principal=principal,
            scopes=effective,
            run_id=run_id,
            audit=audit.bind(
                run_id=run_id, actor_id=principal.id, actor_kind=principal.kind.value
            ),
            clock=clock,
            purpose=purpose,
            granted_by=tuple(granted_by),
            services=services,
        )

    @property
    def store(self):
        """The database handle, for tools and providers. Raises when a session was
        minted without services — which means a caller built one by hand and should
        say what infrastructure it may use."""
        if self.services is None:
            raise RuntimeError(
                "this session carries no runtime services; construct it via "
                "Harness.user_session or pass services= explicitly"
            )
        return self.services.store

    def for_call(self, call_key: str) -> Session:
        """This session, bound to one tool invocation."""
        return replace(self, call_key=call_key)

    def derive_id(self, prefix: str, length: int = 5) -> str:
        """A new identifier that is a deterministic function of this write.

        Records created by tools take their ids from the call's idempotency key
        rather than from a random source. Three things follow, and the third is why
        it is worth the indirection:

        * A replayed write yields the same identifier, so a resumed workflow cannot
          produce a second record that merely *looks* different.
        * Two genuinely distinct writes get distinct ids, because the key already
          distinguishes them by run, step and parameters.
        * A whole run is reproducible. Prompts downstream of a write embed the id it
          produced, so a random id would make every prompt — and every recorded
          model exchange — unrepeatable.
        """
        if not self.call_key:
            raise RuntimeError(
                "derive_id is only available inside a tool invocation; the invoker "
                "binds the call key"
            )
        return f"{prefix}-{short_digest(self.call_key, prefix, length=length)}"

    # --- authorisation ---------------------------------------------------------

    def require(self, *scopes: str, subject: str = "operation") -> None:
        """Raise :class:`ScopeDenied` unless every scope is held.

        The denial is audited before it is raised, so a refusal is as legible in the
        ledger as a success. An agent that quietly declined to act would be worse
        than one that acted wrongly — nobody would know to look.
        """
        required = frozenset(scopes)
        missing = self.scopes.missing(required)
        if missing:
            self.audit.emit(
                EventType.SCOPE_DENIED,
                f"{self.principal.label} may not {subject}",
                subject=subject,
                required=sorted(required),
                missing=sorted(missing),
                held=sorted(self.scopes),
                scope_sources=list(self.granted_by),
            )
            raise ScopeDenied(
                principal_id=self.principal.id, missing=missing, subject=subject
            )

    def can(self, *scopes: str) -> bool:
        """Non-raising, non-auditing check. For deciding what to *offer*, never for
        deciding what to *permit* — permission decisions must leave a trace."""
        return self.scopes.covers(frozenset(scopes))

    # --- derived sessions ------------------------------------------------------

    def downscope(self, scopes: frozenset[str], *, purpose: str) -> Session:
        """A strictly narrower session. Used when handing control to a subsystem
        that needs less than the run does — a workflow step, say."""
        return Session(
            principal=self.principal,
            scopes=self.scopes.intersect(scopes),
            run_id=self.run_id,
            audit=self.audit,
            clock=self.clock,
            purpose=purpose,
            granted_by=(*self.granted_by, "downscope"),
            services=self.services,
        )

    @property
    def is_system(self) -> bool:
        return self.principal.kind is PrincipalKind.SYSTEM


def system_session(
    *,
    principal_id: str,
    scopes: frozenset[str],
    run_id: str,
    audit: AuditWriter,
    clock: Clock,
    purpose: str,
    services: RuntimeServices | None = None,
) -> Session:
    """Mint a narrow non-human session.

    Detectors run on a schedule, while the employee is asleep or out of office, so
    something has to act. That something is deliberately not the user: it is a
    system principal holding read-and-enqueue scopes only. Every use is audited with
    its purpose, because an unexplained system action is the thing an auditor will
    ask about first. DESIGN.md discusses how this maps onto a real deployment.
    """
    principal = Principal(
        id=principal_id,
        kind=PrincipalKind.SYSTEM,
        name=principal_id,
        role="system",
        scopes=ScopeSet(scopes),
    )
    return Session(
        principal=principal,
        scopes=ScopeSet(scopes),
        run_id=run_id,
        audit=audit.bind(
            run_id=run_id, actor_id=principal_id, actor_kind=PrincipalKind.SYSTEM.value
        ),
        clock=clock,
        purpose=purpose,
        granted_by=("system_principal",),
        services=services,
    )
