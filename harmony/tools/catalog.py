"""The tool catalog: what exists, and what this particular session may be offered.

The distinction matters. :meth:`ToolCatalog.available_to` is what the planner is
shown, and it is filtered to the session's scopes and the profile's allow-list.
A model that is never told about ``erp.po.cancel`` cannot propose it, which removes
a whole class of plan that the gate would only have to reject later.

This is convenience, not security. The gate re-checks everything the planner
proposed, because the model can hallucinate a tool name it was never shown and a
compromised prompt could try to. Filtering the catalog reduces noise; the invoker
is what enforces.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence

from harmony.tools.base import TOOLS, ToolSpec


class ToolCatalog:
    """A view over the global tool registry."""

    def __init__(self, registry=TOOLS) -> None:
        self._registry = registry

    def get(self, name: str) -> ToolSpec:
        return self._registry.get(name)

    def has(self, name: str) -> bool:
        return name in self._registry

    def names(self) -> list[str]:
        return self._registry.names()

    def all(self) -> list[ToolSpec]:
        return self._registry.all()

    # --- filtered views --------------------------------------------------------

    def matching(self, patterns: Sequence[str]) -> list[ToolSpec]:
        """Tools whose names match any glob pattern, e.g. ``["erp.*", "mail.send"]``.

        Profiles declare their toolset this way so that adding a tool to a system
        the user already works with does not require editing every profile.
        """
        return [
            spec
            for spec in self.all()
            if any(fnmatch.fnmatch(spec.name, pattern) for pattern in patterns)
        ]

    def available_to(
        self, *, scopes: frozenset[str], patterns: Sequence[str] | None = None
    ) -> list[ToolSpec]:
        """Tools this principal both holds the scopes for and is allowed by profile."""
        candidates = self.matching(patterns) if patterns else self.all()
        return [spec for spec in candidates if scopes.issuperset(spec.scopes)]

    def describe_for_planner(
        self, *, scopes: frozenset[str], patterns: Sequence[str] | None = None
    ) -> list[dict]:
        """The catalog as the planner sees it."""
        return [spec.describe() for spec in self.available_to(scopes=scopes, patterns=patterns)]

    # --- integrity -------------------------------------------------------------

    def validate_references(self) -> list[str]:
        """Every declared compensation must name a tool that exists.

        Called at startup. A workflow that discovers its rollback path is missing
        only when it needs it has already failed at the worst possible moment.
        """
        problems: list[str] = []
        for spec in self.all():
            if spec.compensation and not self.has(spec.compensation):
                problems.append(
                    f"tool '{spec.name}' declares compensation '{spec.compensation}' "
                    "which is not registered"
                )
            if spec.compensation:
                comp = self.get(spec.compensation)
                if not comp.writes:
                    problems.append(
                        f"tool '{spec.name}' compensates via '{spec.compensation}', "
                        "which is not declared as a write"
                    )
        return problems
