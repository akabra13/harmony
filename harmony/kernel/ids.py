"""Identifier and digest helpers.

Two rules shape this module:

* **Ids are prefixed** so an id is self-describing in an audit line. ``RUN-a3f1c2``
  needs no column header to be understood.
* **Digests are canonical.** ``digest()`` is the single definition of "the same
  input" used by idempotency keys, attention-item fingerprints, the audit hash
  chain, and approval binding. If those four disagreed about canonicalisation, the
  bugs would be subtle and awful, so they share one function.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any


def new_id(prefix: str, size: int = 6) -> str:
    """A short, random, prefixed identifier: ``RUN-9f2ab1``."""
    return f"{prefix}-{secrets.token_hex(size)[:size]}"


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace, stable separators.

    Sets and tuples become sorted lists so that two structurally equal payloads
    always serialise identically.
    """
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), default=str)


def _normalise(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(_normalise(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _normalise(v) for k, v in value.items()}
    return value


def digest(*parts: Any) -> str:
    """A stable hex digest over any number of structured parts."""
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(canonical_json(part).encode("utf-8"))
        hasher.update(b"\x1f")  # unit separator: prevents field-boundary collisions
    return hasher.hexdigest()


def short_digest(*parts: Any, length: int = 12) -> str:
    """A truncated digest, for identifiers a human has to read or type."""
    return digest(*parts)[:length]
