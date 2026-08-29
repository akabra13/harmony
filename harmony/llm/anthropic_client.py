"""Anthropic-backed structured completion.

Structured output is obtained by giving the model exactly one tool whose input
schema is the answer we want, and forcing its use. The model cannot reply with
prose because prose is not an available move — which is a stronger guarantee than
asking politely for JSON and parsing whatever arrives.
"""

from __future__ import annotations

import os
import time

from harmony.kernel.errors import LLMOutputInvalid
from harmony.llm.client import LLMRequest, LLMResponse

DEFAULT_MODEL = "claude-sonnet-4-5"
EMIT_TOOL = "emit_result"


class AnthropicClient:
    """Live Anthropic client. The only component that talks to the network."""

    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None:
        from anthropic import Anthropic  # imported lazily: replay runs need no SDK

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Run in replay mode (the default) or "
                "export a key to record fresh cassettes."
            )
        self._client = Anthropic(api_key=key)
        self.model = model or os.environ.get("HARMONY_MODEL", DEFAULT_MODEL)

    def complete_structured(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        message = self._client.messages.create(
            model=self.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system=request.system,
            messages=[{"role": "user", "content": request.prompt}],
            tools=[
                {
                    "name": EMIT_TOOL,
                    "description": (
                        "Emit the result. This is the only way to answer; "
                        "every field of the schema must be supplied."
                    ),
                    "input_schema": request.output_schema,
                }
            ],
            tool_choice={"type": "tool", "name": EMIT_TOOL},
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        for block in message.content:
            if getattr(block, "type", None) == "tool_use" and block.name == EMIT_TOOL:
                return LLMResponse(
                    output=dict(block.input),
                    model=message.model,
                    input_tokens=message.usage.input_tokens,
                    output_tokens=message.usage.output_tokens,
                    latency_ms=latency_ms,
                    stop_reason=message.stop_reason or "",
                )

        raise LLMOutputInvalid(
            "model did not emit the required structured result",
            call_site=request.call_site,
            stop_reason=message.stop_reason,
        )
