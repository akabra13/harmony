"""Finding the deployment the harness should run.

The kernel must not name a customer. It cannot ``import northfield``, and it cannot
carry ``"northfield:DEPLOYMENT"`` as a default string either — a hardcoded default
is a hardcoded dependency wearing a disguise.

So deployments register themselves through a packaging entry point::

    # pyproject.toml
    [project.entry-points."harmony.deployments"]
    northfield = "northfield:DEPLOYMENT"

The CLI discovers what is installed, uses the single one if there is exactly one,
and asks which when there are several. Installing a second company alongside the
first needs no change to this file, and ``tests/architecture`` asserts that the
kernel never mentions the first one.
"""

from __future__ import annotations

import os
from importlib.metadata import entry_points

from harmony.runtime.deployment import Deployment

ENTRY_POINT_GROUP = "harmony.deployments"
ENV_VAR = "HARMONY_DEPLOYMENT"


def available_deployments() -> dict[str, str]:
    """Registered deployment names, mapped to the object they load."""
    return {ep.name: ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)}


def load_deployment(name: str | None = None) -> Deployment:
    """Load a deployment by name, by environment variable, or by being the only one."""
    name = name or os.environ.get(ENV_VAR)
    registered = list(entry_points(group=ENTRY_POINT_GROUP))

    if not registered:
        raise RuntimeError(
            f"no deployment is registered under the '{ENTRY_POINT_GROUP}' entry point "
            "group. Install a company package (`pip install -e .`) that declares one."
        )

    if name:
        for ep in registered:
            if ep.name == name:
                return _validate(ep.load(), ep.name)
        raise RuntimeError(
            f"no deployment named '{name}'. Available: {sorted(e.name for e in registered)}"
        )

    if len(registered) == 1:
        return _validate(registered[0].load(), registered[0].name)

    raise RuntimeError(
        f"several deployments are installed: {sorted(e.name for e in registered)}. "
        f"Choose one with {ENV_VAR}=<name>."
    )


def _validate(candidate: object, name: str) -> Deployment:
    if not isinstance(candidate, Deployment):
        raise RuntimeError(
            f"entry point '{name}' resolved to {type(candidate).__name__}, "
            "not a Deployment"
        )
    return candidate
