"""Generate the cassettes the demo replays.

Two modes, and the difference matters:

``python scripts/author_cassettes.py``
    Runs every scenario against the scripted fixtures in
    ``northfield/demo/scripted_answers.py`` and writes real cassette files. No API
    key, no cost. This is what ships, so that a clean checkout runs ``make demo``
    immediately.

``HARMONY_LLM=record python scripts/author_cassettes.py``
    Runs every scenario against the live model and overwrites the cassettes with
    genuine recordings. Needs ``ANTHROPIC_API_KEY``.

Either way the output is the same format and the demo cannot tell the difference,
which is the point: the fixtures are a stand-in for a recording, not a different
code path. Each cassette records ``source`` so a reader can tell which they are
looking at.

Cassette keys include the prompt, so editing a prompt orphans its cassette and the
replay run fails loudly. Re-run this script after any prompt change.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from harmony.kernel.config import load_env  # noqa: E402

load_env(REPO)  # so `.env` works here exactly as it does for the CLI

from rich.console import Console  # noqa: E402

from harmony.llm.replay import CassetteLibrary, RecordingClient  # noqa: E402
from northfield.demo.scripted_answers import scripted_client  # noqa: E402

CASSETTE_DIR = REPO / "cassettes"


def build_authoring_client(library: CassetteLibrary):
    """The client that will answer, wrapped so every exchange is written out."""
    mode = os.environ.get("HARMONY_LLM", "fixture").lower()
    if mode in ("record", "live"):
        from harmony.llm.anthropic_client import AnthropicClient

        return RecordingClient(AnthropicClient(), library), "recorded"
    return RecordingClient(scripted_client(), library), "fixture"


def main() -> int:
    # The scenarios narrate as they go; authoring does not want the narration, and
    # a StringIO sink also sidesteps the console encoding entirely.
    console = Console(file=io.StringIO(), width=100)
    report = Console()

    if CASSETTE_DIR.exists():
        shutil.rmtree(CASSETTE_DIR)
    library = CassetteLibrary(CASSETTE_DIR)
    client, source = build_authoring_client(library)

    report.print(f"authoring cassettes from [bold]{source}[/bold] answers…")

    from northfield.demo.failures import run_failures
    from northfield.demo.scenario_a import run_scenario_a
    from northfield.demo.scenario_b import run_scenario_b

    workdir = Path(tempfile.mkdtemp(prefix="harmony-cassettes-"))
    for name, scenario in (
        ("scenario-a", run_scenario_a),
        ("scenario-b", run_scenario_b),
        ("failures", run_failures),
    ):
        report.print(f"  · {name}")
        scenario(console, db_path=str(workdir / f"{name}.db"), llm=client)

    # Stamp the provenance so a reader can tell a fixture from a recording.
    import json

    for path in sorted(CASSETTE_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source"] = source
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    count = len(list(CASSETTE_DIR.glob("*.json")))
    report.print(f"[green]wrote {count} cassette(s) to {CASSETTE_DIR}[/green]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
