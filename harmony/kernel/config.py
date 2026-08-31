"""Loading configuration from a ``.env`` file.

Shipping a ``.env.example`` that nothing reads is worse than shipping none at all:
it tells a reviewer where to put their API key, they put it there, and the harness
silently carries on in replay mode. This module is what makes the file mean
something.

Two rules:

* **The environment wins.** An explicitly exported variable is never overridden by
  the file. Somebody running ``HARMONY_LLM=live harmony eval`` has said what they
  want, and a config file quietly contradicting them is the kind of thing that
  costs an afternoon.
* **Loading is idempotent and silent.** Called from every entry point — the CLI and
  both scripts — because a script that bypassed it would behave differently from
  the CLI for no reason a user could see.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILENAME = ".env"


def load_env(start: Path | None = None) -> Path | None:
    """Load ``.env`` from the working directory or the nearest parent.

    Returns the file loaded, or ``None`` if there was none — which is the normal
    case, since the demo and the tests need no configuration at all.

    Searching upwards means ``harmony`` works from a subdirectory of the repo, which
    is where people actually run things from.
    """
    path = _find(start or Path.cwd())
    if path is None:
        return None

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        return _load_manually(path)

    load_dotenv(path, override=False)
    return path


def _find(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / ENV_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _load_manually(path: Path) -> Path:
    """A minimal fallback if python-dotenv is somehow absent.

    Handles the shape ``.env.example`` actually uses — ``KEY=value``, comments,
    blank lines, optional surrounding quotes — and nothing more. It exists so a
    missing optional dependency degrades to "works for the simple case" rather than
    to "silently ignores your key".
    """
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
    return path
