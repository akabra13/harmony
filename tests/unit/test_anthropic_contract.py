"""The live model client's contract, checked without a network call.

**This is not a substitute for running against the real API.** The repo ships
cassettes authored from scripted fixtures, so until `make record` is run with a key
the live path has never actually executed. What these tests do is remove the most
likely reasons it would fail on first contact:

* a generated JSON Schema the API rejects as a tool ``input_schema``;
* a request that forgets to force the tool, letting the model reply with prose;
* a response parser that misses the ``tool_use`` block.

Each of those is a silent bug in replay and an immediate 400 or a crash live. A
fake transport catches all three for the price of no tokens.
"""

from __future__ import annotations

import os
import re
from typing import Any

import pytest

from harmony.kernel.errors import LLMOutputInvalid
from harmony.llm.anthropic_client import EMIT_TOOL, AnthropicClient
from harmony.llm.client import LLMRequest
from harmony.plan.models import PlannerOutput
from northfield.providers.mail import SupplierCommitment


# --- the schemas we ask the API to accept --------------------------------------


def _output_models():
    """Every model the harness asks the API to fill in.

    The two static ones, plus a representative dynamically-built workflow step
    schema — that last one is constructed at run time from YAML and is the most
    likely to be malformed, precisely because nobody reads it.
    """
    from harmony.workflow.models import FieldSpec, Step, StepKind

    step = Step(
        id="choose_supplier",
        kind=StepKind.LLM,
        prompt="x",
        output_schema={
            "supplier_id": FieldSpec(type="string", enum_from="steps.x.output.ids"),
            "justification": FieldSpec(type="string", max_length=400),
        },
    )
    dynamic = step.output_model("test", {"supplier_id": ["S-Y", "S-Z"]})
    return [PlannerOutput, SupplierCommitment, dynamic]


@pytest.mark.parametrize("model", _output_models(), ids=lambda m: m.__name__)
def test_every_output_schema_is_a_valid_tool_input_schema(model):
    """Anthropic requires a tool's ``input_schema`` to be a JSON Schema object with
    ``properties``. Pydantic satisfies that, but a nested model produces ``$defs``
    and ``$ref``, and it is worth asserting the top level stays the shape the API
    wants rather than discovering it from a 400."""
    schema = model.model_json_schema()

    assert schema.get("type") == "object", f"{model.__name__} is not an object schema"
    assert "properties" in schema, f"{model.__name__} declares no properties"
    assert schema["properties"], f"{model.__name__} has an empty property set"


def test_the_constrained_choice_reaches_the_schema_as_an_enum():
    """The workflow's ``enum_from`` is the mechanism that stops a model choosing a
    supplier nobody offered. If it stopped producing an enum the guardrail would
    still catch it, but only after a wasted call — and the schema is the cheaper of
    the two defences."""
    _, _, dynamic = _output_models()
    field = dynamic.model_json_schema()["properties"]["supplier_id"]

    allowed = field.get("enum") or field.get("const")
    assert allowed, f"supplier_id is not constrained: {field}"
    assert set(allowed if isinstance(allowed, list) else [allowed]) == {"S-Y", "S-Z"}


# --- the request we send, and the response we parse -----------------------------


class _FakeMessages:
    """Stands in for ``anthropic.Anthropic().messages``.

    It validates every keyword against the *real* SDK signature before answering.
    An earlier version of this fake accepted ``**kwargs`` unconditionally, and so
    happily swallowed a ``temperature=`` argument that the installed SDK had
    dropped — the tests passed and the first live call died with a TypeError. A
    fake more permissive than the thing it stands in for is worse than no fake: it
    converts a loud failure into a false pass.
    """

    def __init__(self, content: list[Any], stop_reason: str = "tool_use") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.received: dict[str, Any] = {}

    def create(self, **kwargs: Any):
        _assert_accepted_by_the_sdk(kwargs)
        self.received = kwargs
        return type(
            "Message",
            (),
            {
                "content": self.content,
                "model": kwargs["model"],
                "stop_reason": self.stop_reason,
                "usage": type("Usage", (), {"input_tokens": 11, "output_tokens": 22})(),
            },
        )()


def _assert_accepted_by_the_sdk(kwargs: dict[str, Any]) -> None:
    """Reject any keyword the installed SDK's Messages.create does not accept."""
    import inspect

    from anthropic.resources.messages import Messages

    accepted = set(inspect.signature(Messages.create).parameters)
    unknown = sorted(set(kwargs) - accepted)
    assert not unknown, (
        f"the request passes {unknown}, which anthropic "
        f"{__import__('anthropic').__version__} does not accept. "
        "Sampling parameters were removed from current models; see "
        "harmony/llm/anthropic_client.py."
    )


def _tool_use_block(payload: dict[str, Any], name: str = EMIT_TOOL):
    return type("Block", (), {"type": "tool_use", "name": name, "input": payload})()


def _client(messages: _FakeMessages) -> AnthropicClient:
    client = AnthropicClient.__new__(AnthropicClient)
    client._client = type("Anthropic", (), {"messages": messages})()
    client.model = "claude-opus-5"
    return client


def _request() -> LLMRequest:
    return LLMRequest(
        call_site="planner",
        system="you are a planner",
        prompt="what should we do?",
        output_schema=PlannerOutput.model_json_schema(),
        max_tokens=1234,
    )


def test_the_request_forces_the_tool_so_prose_is_not_an_option():
    """Structured output here is not a request the model may decline. Asking
    politely for JSON and parsing whatever arrives is the failure mode this avoids,
    and it only shows up under load."""
    messages = _FakeMessages([_tool_use_block({"summary": "s"})])
    _client(messages).complete_structured(_request())

    sent = messages.received
    assert sent["tool_choice"] == {"type": "tool", "name": EMIT_TOOL}
    assert [t["name"] for t in sent["tools"]] == [EMIT_TOOL]
    assert sent["tools"][0]["input_schema"] == PlannerOutput.model_json_schema()


def test_the_request_carries_the_system_prompt_and_settings():
    messages = _FakeMessages([_tool_use_block({"summary": "s"})])
    _client(messages).complete_structured(_request())

    sent = messages.received
    assert sent["system"] == "you are a planner"
    assert sent["messages"] == [{"role": "user", "content": "what should we do?"}]
    assert sent["max_tokens"] == 1234


def test_the_request_sends_no_sampling_parameters():
    """They were removed from current models and are rejected outright.

    Determinism comes from replay, not from pinning temperature to zero — which is
    why the evaluation suite asserts on properties of the decision rather than on
    its wording.
    """
    messages = _FakeMessages([_tool_use_block({"summary": "s"})])
    _client(messages).complete_structured(_request())

    for removed in ("temperature", "top_p", "top_k"):
        assert removed not in messages.received


def test_the_default_model_is_one_the_api_still_serves():
    """A stale default is a first-call 404 for anyone who does not set the env var."""
    from harmony.llm.anthropic_client import DEFAULT_MODEL

    assert DEFAULT_MODEL == "claude-opus-5"
    assert not re.search(r"-\d{8}$", DEFAULT_MODEL), (
        "model ids take no date suffix"
    )


def test_the_tool_use_block_is_parsed_into_the_response():
    payload = {"summary": "reroute it", "action_kind": "workflow"}
    messages = _FakeMessages([_tool_use_block(payload)])

    response = _client(messages).complete_structured(_request())

    assert response.output == payload
    assert response.input_tokens == 11
    assert response.output_tokens == 22
    assert response.from_cassette is False


def test_a_reply_without_the_tool_block_fails_loudly():
    """If the model talks instead of emitting, that is a failure, not something to
    salvage with a regex."""
    text_block = type("Block", (), {"type": "text", "text": "I think you should..."})()
    messages = _FakeMessages([text_block], stop_reason="end_turn")

    with pytest.raises(LLMOutputInvalid):
        _client(messages).complete_structured(_request())


def test_a_block_naming_a_different_tool_is_not_accepted():
    """Defensive, but cheap: only the emit tool's input is our answer."""
    messages = _FakeMessages([_tool_use_block({"x": 1}, name="something_else")])

    with pytest.raises(LLMOutputInvalid):
        _client(messages).complete_structured(_request())


def test_a_missing_api_key_fails_with_an_actionable_message():
    """The first thing a reviewer hits if they try `make record` without a key."""
    import os

    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            AnthropicClient()
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


# --- configuration --------------------------------------------------------------


def test_a_dotenv_file_reaches_the_process(tmp_path, monkeypatch):
    """`.env.example` tells a reviewer where to put their key.

    Shipping that file with nothing reading it is worse than shipping none: they
    put the key where they were told, and the harness silently carries on in replay
    mode. This asserts the file is actually loaded.
    """
    from harmony.kernel.config import load_env

    (tmp_path / ".env").write_text(
        "# a comment\n"
        "ANTHROPIC_API_KEY=sk-ant-from-file\n"
        'HARMONY_MODEL="claude-opus-5"\n'
        "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HARMONY_MODEL", raising=False)

    loaded = load_env(tmp_path)

    assert loaded == tmp_path / ".env"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-file"
    assert os.environ["HARMONY_MODEL"] == "claude-opus-5", "quotes should be stripped"


def test_an_exported_variable_beats_the_file(tmp_path, monkeypatch):
    """Somebody running `HARMONY_LLM=live ...` has said what they want, and a config
    file quietly contradicting them costs an afternoon."""
    from harmony.kernel.config import load_env

    (tmp_path / ".env").write_text("HARMONY_LLM=replay\n", encoding="utf-8")
    monkeypatch.setenv("HARMONY_LLM", "live")

    load_env(tmp_path)

    assert os.environ["HARMONY_LLM"] == "live"


def test_no_dotenv_is_the_normal_case(tmp_path):
    """The demo and the tests need no configuration at all."""
    from harmony.kernel.config import load_env

    assert load_env(tmp_path) is None


def test_the_shipped_example_documents_every_variable_the_code_reads():
    """A variable the code honours but the example never mentions is one nobody
    will discover."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    example = (repo / ".env.example").read_text(encoding="utf-8")

    read_by_code = set()
    for path in (repo / "harmony").rglob("*.py"):
        read_by_code |= set(
            re.findall(r'environ\.get\(\s*"(HARMONY_[A-Z_]+|ANTHROPIC_[A-Z_]+)"', path.read_text(encoding="utf-8"))
        )

    undocumented = {name for name in read_by_code if name not in example}
    assert not undocumented, f".env.example never mentions {sorted(undocumented)}"


def test_doctor_distinguishes_replay_from_live(monkeypatch, capsys):
    """"Am I actually calling the API?" must be answerable without reading source.

    Getting it wrong in either direction is expensive: you either believe you
    validated the prompts when you replayed a fixture, or you spend money believing
    you are offline.
    """
    from typer.testing import CliRunner

    from harmony.cli.main import app

    runner = CliRunner()

    monkeypatch.setenv("HARMONY_LLM", "replay")
    replay = runner.invoke(app, ["doctor"])
    assert replay.exit_code == 0
    assert "REPLAY" in replay.stdout

    monkeypatch.setenv("HARMONY_LLM", "live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    live = runner.invoke(app, ["doctor"])
    assert live.exit_code == 0
    assert "LIVE" in live.stdout


def test_doctor_fails_when_live_is_configured_without_a_key(monkeypatch):
    """Better to fail here than on the first call, halfway through a scenario."""
    from typer.testing import CliRunner

    from harmony.cli.main import app

    monkeypatch.setenv("HARMONY_LLM", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.stdout


def test_a_date_suffixed_model_id_is_rejected_at_construction(monkeypatch):
    """`claude-haiku-4-5-20251001` is a stale convention that still looks plausible.

    Caught here rather than as a 404 partway through a scenario, where the error
    names the HTTP status and not the cause.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("HARMONY_MODEL", "claude-haiku-4-5-20251001")

    with pytest.raises(RuntimeError, match="date suffix"):
        AnthropicClient()

    monkeypatch.setenv("HARMONY_MODEL", "claude-haiku-4-5")
    assert AnthropicClient().model == "claude-haiku-4-5"


def test_a_configured_workspace_is_sent_as_a_header(monkeypatch):
    """Identity-linked keys must name the workspace each request acts in. The SDK
    has no parameter for it, so it rides on default_headers."""
    from harmony.llm.anthropic_client import WORKSPACE_HEADER

    captured: dict[str, Any] = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.messages = _FakeMessages([_tool_use_block({"summary": "s"})])

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)

    client = AnthropicClient()

    assert client.workspace_id == "wrkspc_123"
    assert captured["default_headers"] == {WORKSPACE_HEADER: "wrkspc_123"}


def test_no_workspace_configured_sends_no_header(monkeypatch):
    """Most keys do not need it, and an empty header is not the same as none."""
    captured: dict[str, Any] = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.messages = _FakeMessages([])

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)

    AnthropicClient()

    assert captured["default_headers"] is None


def test_the_workspace_400_is_translated_into_something_actionable(monkeypatch):
    """A stack trace ending in _base_client.py says the request failed and nothing
    about what to change. This one is a line in .env."""
    from harmony.llm.anthropic_client import WORKSPACE_HEADER

    class _Failing:
        def create(self, **kwargs: Any):
            raise RuntimeError(
                f"Error code: 400 - {WORKSPACE_HEADER} is required when "
                "authenticating with an identity-linked API key"
            )

    class _FakeAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            self.messages = _Failing()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)

    with pytest.raises(RuntimeError, match="ANTHROPIC_WORKSPACE_ID"):
        AnthropicClient().complete_structured(_request())
