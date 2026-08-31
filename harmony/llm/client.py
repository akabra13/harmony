"""The model client interface.

Every model call in the harness is *structured*: it declares the JSON Schema of the
answer it will accept, and anything else is a failure rather than a surprise. There
is no free-text completion API here, because there is no call site that wants one.
The planner returns a Proposal; the extractor returns a SupplierCommitment; the
workflow node returns a choice from an enum. Prose is an input, never an output the
harness acts on.

Three implementations share this interface:

:class:`~harmony.llm.anthropic_client.AnthropicClient`
    The real thing.

:class:`~harmony.llm.replay.RecordingClient`
    Wraps a live client and writes every exchange to a cassette.

:class:`~harmony.llm.replay.ReplayClient`
    Serves recorded exchanges by prompt hash. Tests and the default demo run on
    this: no API key, no cost, and a recorded run that a reviewer can reproduce
    byte for byte.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from harmony.kernel.ids import digest


class LLMRequest(BaseModel):
    """One structured model call."""

    call_site: str
    """Which of the harness's model call sites this is: ``planner``,
    ``mail.extract_commitment``, ``workflow.po_reroute.choose_supplier``. Recorded
    in the audit and used to group cassettes, so the LLM boundary table in the
    README can be checked against what actually ran."""

    system: str
    prompt: str
    output_schema: dict[str, Any]
    max_tokens: int = 2048
    temperature: float = 0.0
    """Declared intent, not sent to current Claude models — sampling parameters were
    removed from them and are rejected. Kept because a provider that still honours
    one should get zero: variance buys nothing at any of these call sites.
    Reproducibility comes from replay instead; see harmony/llm/replay.py."""

    def cassette_key(self) -> str:
        """Stable identity of this exchange, for record/replay."""
        return digest(self.call_site, self.system, self.prompt, self.output_schema)


class LLMResponse(BaseModel):
    """What came back, plus what it cost."""

    output: dict[str, Any]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    from_cassette: bool = False
    stop_reason: str = ""

    def usage(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "from_cassette": self.from_cassette,
        }


@runtime_checkable
class LLMClient(Protocol):
    """The one method the harness needs from a model provider."""

    def complete_structured(self, request: LLMRequest) -> LLMResponse: ...


class StubClient:
    """A client that returns canned answers, keyed by call site.

    For unit tests of everything that is not the model: the gate, the workflow
    engine, dedupe. Those tests should not depend on a cassette file, and they
    should fail loudly if a code path they did not expect reaches for the model.
    """

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[LLMRequest] = []

    def complete_structured(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if request.call_site not in self.answers:
            raise AssertionError(
                f"StubClient has no answer for call site '{request.call_site}'; "
                f"known: {sorted(self.answers)}"
            )
        answer = self.answers[request.call_site]
        if callable(answer):
            answer = answer(request)
        return LLMResponse(output=answer, model="scripted-fixture", from_cassette=True)
