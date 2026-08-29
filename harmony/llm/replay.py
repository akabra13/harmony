"""Record and replay of model exchanges.

The demo, the tests and the recorded run in ``docs/runs/`` all execute the same
code path as a live run, but read the model's side of the conversation from disk.
That buys three things worth having:

* **A reviewer can reproduce the recorded run**, exactly, without a key or a bill.
* **Tests are deterministic.** A gate test that fails because the model phrased its
  justification differently is a test nobody trusts.
* **Prompt changes become visible.** The cassette key includes the prompt, so
  editing a prompt orphans its cassette and the replay run fails loudly instead of
  silently answering the old question.

Cassettes are plain JSON, one file per exchange, holding the prompt as well as the
response. They are readable artifacts: ``cassettes/planner-a3f1.json`` is a record
of what the agent was asked and what it said, which is useful in review quite apart
from its role in testing.
"""

from __future__ import annotations

import json
from pathlib import Path

from harmony.kernel.errors import CassetteMiss
from harmony.llm.client import LLMClient, LLMRequest, LLMResponse

DEFAULT_CASSETTE_DIR = Path("cassettes")


class CassetteLibrary:
    """Reads and writes cassette files."""

    def __init__(self, directory: Path | str = DEFAULT_CASSETTE_DIR) -> None:
        self.directory = Path(directory)

    def path_for(self, request: LLMRequest) -> Path:
        safe_site = request.call_site.replace(".", "-").replace("/", "-")
        return self.directory / f"{safe_site}.{request.cassette_key()[:12]}.json"

    def read(self, request: LLMRequest) -> LLMResponse | None:
        path = self.path_for(request)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return LLMResponse(**data["response"], from_cassette=True)

    def write(self, request: LLMRequest, response: LLMResponse) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(request)
        path.write_text(
            json.dumps(
                {
                    "call_site": request.call_site,
                    "key": request.cassette_key(),
                    "request": {
                        "system": request.system,
                        "prompt": request.prompt,
                        "output_schema": request.output_schema,
                    },
                    "response": response.model_dump(exclude={"from_cassette"}),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path


class ReplayClient:
    """Serves recorded exchanges. Raises on a miss rather than improvising."""

    def __init__(self, library: CassetteLibrary | None = None) -> None:
        self._library = library or CassetteLibrary()
        self.hits = 0

    def complete_structured(self, request: LLMRequest) -> LLMResponse:
        response = self._library.read(request)
        if response is None:
            raise CassetteMiss(
                f"no cassette for call site '{request.call_site}'. "
                "The prompt or schema changed; re-record with HARMONY_LLM=live.",
                call_site=request.call_site,
                expected_path=str(self._library.path_for(request)),
            )
        self.hits += 1
        return response


class RecordingClient:
    """Calls a live client and tees every exchange to a cassette."""

    def __init__(self, inner: LLMClient, library: CassetteLibrary | None = None) -> None:
        self._inner = inner
        self._library = library or CassetteLibrary()
        self.recorded: list[Path] = []

    def complete_structured(self, request: LLMRequest) -> LLMResponse:
        response = self._inner.complete_structured(request)
        self.recorded.append(self._library.write(request, response))
        return response


def build_client(
    mode: str | None = None, *, cassette_dir: Path | str = DEFAULT_CASSETTE_DIR
) -> LLMClient:
    """Construct the client for the configured mode.

    ``replay`` (the default) reads cassettes; ``live`` calls the API; ``record``
    calls the API and saves what comes back. Selected by ``HARMONY_LLM``.
    """
    mode = (mode or __import__("os").environ.get("HARMONY_LLM", "replay")).lower()
    library = CassetteLibrary(cassette_dir)

    if mode == "replay":
        return ReplayClient(library)
    if mode in ("live", "record"):
        from harmony.llm.anthropic_client import AnthropicClient

        live = AnthropicClient()
        return RecordingClient(live, library) if mode == "record" else live
    raise ValueError(f"unknown HARMONY_LLM mode '{mode}'; expected replay, live or record")
