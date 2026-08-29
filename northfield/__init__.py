"""Northfield Manufacturing — the company this harness is deployed for.

Everything specific to a manufacturer lives under this package: the systems of
record, the people, the policies, the detectors that watch, the tools that act, and
the workflows purchasing insists on. The harness imports none of it directly. It
receives :data:`DEPLOYMENT` and reads the company through that one seam.

Swapping this package swaps the customer. ``tests/architecture/`` asserts the claim
rather than leaving it as a hope.
"""

from __future__ import annotations

from pathlib import Path

from harmony.runtime.deployment import Deployment
from northfield.directory import directory_factory
from northfield.migrations import NORTHFIELD_MIGRATIONS
from northfield.demo.failures import run_failures
from northfield.demo.scenario_a import run_scenario_a
from northfield.demo.scenario_b import run_scenario_b
from northfield.providers.calendar import availability_factory
from northfield.seed import seed

PACKAGE_ROOT = Path(__file__).parent

DEPLOYMENT = Deployment(
    name="Northfield Manufacturing",
    plugin_package="northfield",
    migrations=NORTHFIELD_MIGRATIONS,
    profiles_dir=PACKAGE_ROOT / "profiles",
    workflows_dir=PACKAGE_ROOT / "workflows",
    policy_path=PACKAGE_ROOT / "policy.yaml",
    seed=seed,
    directory_factory=directory_factory,
    availability_factory=availability_factory,
    demos={
        "scenario-a": run_scenario_a,
        "scenario-b": run_scenario_b,
        "failures": run_failures,
    },
    system_principal_id="system:harmony",
    # Read and enqueue. Deliberately no write scope of any kind — see policy.yaml.
    system_scopes=frozenset({"calendar:freebusy:read", "harmony:schedule:create"}),
)

__all__ = ["DEPLOYMENT"]
