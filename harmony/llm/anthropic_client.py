"""Anthropic-backed structured completion.

Structured output is obtained by giving the model exactly one tool whose input
schema is the answer we want, and forcing its use. The model cannot reply with
prose because prose is not an available move — which is a stronger guarantee than
asking politely for JSON and parsing whatever arrives.

**No sampling parameters.** `temperature`, `top_p` and `top_k` were removed from
current Claude models — they are rejected outright — so determinism cannot come
from pinning temperature to zero. It comes from replay: `cassettes/` freezes the
model's side, which is what makes tests and the recorded run reproducible. A live
run genuinely varies, and that is precisely why the evaluation suite in
`harmony/eval/` asserts on properties of the decision rather than on its wording.
`LLMRequest.temperature` is retained as a declaration of intent for clients that
still honour it; this one does not send it.

**Workspace routing.** An identity-linked API key must say which workspace a
request acts in, via an ``anthropic-workspace-id`` header. The SDK has no
parameter for it, so it goes through ``default_headers``. Keys that are not
identity-linked ignore it, which is why sending it whenever it is configured is
safe and omitting it is only sometimes safe.
"""

from __future__ import annotations

import os
import time

from harmony.kernel.errors import LLMOutputInvalid
from harmony.llm.client import LLMRequest, LLMResponse

DEFAULT_MODEL = "claude-opus-5"
EMIT_TOOL = "emit_result"
WORKSPACE_HEADER = "anthropic-workspace-id"


def _validated_model(model: str) -> str:
    """Reject a model id with a date suffix.

    Current ids are complete as they stand — ``claude-haiku-4-5``, not
    ``claude-haiku-4-5-20251001``. The dated form is a stale convention that still
    looks plausible, and the failure it produces is a 404 partway through a
    scenario rather than anything that names the cause.
    """
    import re

    if re.search(r"-\d{8}$", model):
        raise RuntimeError(
            f"HARMONY_MODEL is '{model}', which carries a date suffix. "
            f"Current model ids take none — use '{re.sub(r'-[0-9]{8}$', '', model)}'."
        )
    return model


class AnthropicClient:
    """Live Anthropic client. The only component that talks to the network."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        from anthropic import Anthropic  # imported lazily: replay runs need no SDK

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Run in replay mode (the default) or "
                "export a key to record fresh cassettes."
            )
        workspace = workspace_id or os.environ.get("ANTHROPIC_WORKSPACE_ID") or ""
        self.workspace_id = workspace.strip()
        self._client = Anthropic(
            api_key=key,
            default_headers=(
                {WORKSPACE_HEADER: self.workspace_id} if self.workspace_id else None
            ),
        )
        self.model = _validated_model(model or os.environ.get("HARMONY_MODEL", DEFAULT_MODEL))

    def complete_structured(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        try:
            message = self._create(request)
        except Exception as exc:  # noqa: BLE001 - re-raised, possibly annotated
            raise self._explain(exc) from exc
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

    def _explain(self, exc: Exception) -> Exception:
        """Turn the API's account-shaped 400s into something actionable.

        A stack trace ending in ``_base_client.py`` tells a reader the request
        failed and nothing about what to change. These two are configuration, not
        code, and the fix is a line in ``.env``.
        """
        message = str(exc)
        if WORKSPACE_HEADER in message and not self.workspace_id:
            return RuntimeError(
                "This API key is identity-linked and must name the workspace it "
                "acts in. Set ANTHROPIC_WORKSPACE_ID in .env — you can find the id "
                "in the Anthropic Console under the workspace's settings."
            )
        if "model" in message.lower() and "not_found" in message.lower():
            return RuntimeError(
                f"The API does not recognise model '{self.model}'. Check "
                "HARMONY_MODEL in .env; ids take no date suffix."
            )
        return exc

    def _create(self, request: LLMRequest):
        return self._client.messages.create(
            model=self.model,
            max_tokens=request.max_tokens,
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
