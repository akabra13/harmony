"""Agent profiles: what one person's agent is made of.

"Every employee at a manufacturer gets a personal AI agent." A profile is what
makes that a configuration statement rather than a code statement. It binds a set
of people to the detectors that watch on their behalf, the systems their agent
reads, the tools it may propose, and the workflows it may enter.

The consequence worth noticing: **Scenario B needed a new profile, not a new
orchestrator.** A quality manager's agent watches different things, reads a
different system and proposes different actions, and every one of those differences
is a line of YAML. The loop that runs them is the same loop.

``scopes`` is the profile's *declared need*, and it is intersected with the user's
entitlements when a session is minted. A profile cannot grant anything — it can only
narrow. A purchasing manager entitled to cancel purchase orders who runs a profile
that never declared that scope does not have it for that run, which keeps a
compromised prompt inside the profile's blast radius rather than the person's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from harmony.kernel.errors import NotRegistered
from harmony.kernel.registry import Registry


class AgentProfile(BaseModel):
    """The declarative definition of one kind of employee's agent."""

    id: str
    name: str
    description: str = ""
    assigned_to: list[str] = Field(default_factory=list)
    """User ids whose agent this is."""

    detectors: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    """Systems the agent gathers context from, in the order they are read."""

    tools: list[str] = Field(default_factory=list)
    """Glob patterns over tool names: ``["erp.*", "mail.send"]``. Patterns rather
    than exact names so that adding a tool to a system the role already works with
    does not require touching every profile."""

    workflows: list[str] = Field(default_factory=list)
    """Declared workflows this agent may enter. Empty means the free-form path only
    — which is exactly how the quality manager's profile is configured."""

    scopes: list[str] = Field(default_factory=list)
    """What this profile needs. Intersected with the user's entitlements, never
    added to them."""

    source_path: str = ""

    def serves(self, user_id: str) -> bool:
        return user_id in self.assigned_to

    def scope_set(self) -> frozenset[str] | None:
        """``None`` when the profile declares no constraint, which leaves the user's
        entitlements untouched rather than intersecting them to nothing."""
        return frozenset(self.scopes) if self.scopes else None

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "detectors": self.detectors,
            "providers": self.providers,
            "tools": self.tools,
            "workflows": self.workflows or ["(free-form planning only)"],
        }


class ProfileCatalog:
    """Profiles, by id, with lookup by the user they serve."""

    def __init__(self) -> None:
        self._registry: Registry[AgentProfile] = Registry("agent profile")

    def add(self, profile: AgentProfile) -> AgentProfile:
        return self._registry.register(profile.id, profile)

    def get(self, profile_id: str) -> AgentProfile:
        return self._registry.get(profile_id)

    def all(self) -> list[AgentProfile]:
        return self._registry.all()

    def ids(self) -> list[str]:
        return self._registry.names()

    def for_user(self, user_id: str) -> AgentProfile:
        """The profile serving a user.

        Exactly one, deliberately. A person with two agents would have two sets of
        detectors racing to raise the same attention item, and dedupe keys on the
        detector id, so both would fire. Multiple profiles per person is a real
        requirement eventually; it needs a merge rule, and the README lists it under
        what was cut.
        """
        matches = [p for p in self.all() if p.serves(user_id)]
        if not matches:
            raise NotRegistered(
                f"no agent profile is assigned to user '{user_id}'",
                user_id=user_id,
                profiles=self.ids(),
            )
        if len(matches) > 1:
            raise NotRegistered(
                f"user '{user_id}' is assigned to more than one profile: "
                f"{[p.id for p in matches]}",
                user_id=user_id,
            )
        return matches[0]


def load_profiles(directory: Path | str) -> ProfileCatalog:
    """Load every ``*.yaml`` profile in a directory."""
    catalog = ProfileCatalog()
    for path in sorted(Path(directory).glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        catalog.add(AgentProfile(**raw, source_path=str(path)))
    return catalog
