"""The deployment record: everything the harness needs to know about one company.

This is the seam. The kernel imports nothing from ``northfield``; instead the
company package hands the harness one of these, and the harness reads its systems,
its people and its policies through it.

Swapping this record swaps the customer. That is the claim the architecture makes,
and it is checkable: ``tests/architecture/test_kernel_purity.py`` asserts that no
module under ``harmony/`` mentions a manufacturing noun, and the only import edge
between the two packages runs through here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harmony.kernel.store import Migration, Store

if TYPE_CHECKING:
    from harmony.gate.approvals import AvailabilityOracle
    from harmony.identity.directory import UserDirectory


@dataclass(frozen=True)
class Deployment:
    """One company's binding of the harness to its own systems."""

    name: str
    plugin_package: str
    """Imported at start-up so decorators register this company's detectors,
    providers, tools and gate rules. Nothing else discovers them."""

    migrations: list[Migration]
    """Schema for this company's systems of record. Applied alongside the harness's
    own, and kept separate so the two are never confused."""

    profiles_dir: Path
    workflows_dir: Path
    policy_path: Path

    seed: Callable[[Store], None]
    """Populates the systems of record. In a real deployment there is nothing to
    seed — the systems already exist and the connectors point at them."""

    directory_factory: Callable[[Store], "UserDirectory"]
    availability_factory: Callable[[Any], "AvailabilityOracle"]
    """Given the harness, returns something that can answer whether a person is
    available on a day. Northfield answers from its calendar; a real deployment
    would answer from Microsoft Graph."""

    demos: dict[str, Callable[..., Any]] = field(default_factory=dict)
    """Scripted narratives this company ships, by name. The CLI lists and dispatches
    them from here rather than importing them, so the kernel never learns a
    scenario's name — the same reason it never learns a supplier's."""

    system_principal_id: str = "system:harmony"
    system_scopes: frozenset[str] = field(default_factory=frozenset)
    """What the non-human principal may do while the employee is asleep. Read and
    enqueue only — see DESIGN.md on why this identity must never be able to write."""
