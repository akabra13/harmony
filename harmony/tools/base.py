"""Tool definitions: what the agent is able to do, and what it costs to be allowed.

A tool is a typed, scoped, individually auditable action. The declaration carries
everything the harness needs to run it safely, so the invoker never has to ask the
tool anything at call time:

* ``input``/``output`` — pydantic models. These do triple duty: they validate what
  the planner proposed, they generate the JSON Schema the model is shown, and they
  document the tool.
* ``scopes`` — what the acting principal must hold. Checked by the invoker.
* ``writes`` — whether this tool changes a system of record. Drives the gate's
  "every write needs a human" rule, so a tool author cannot forget to ask.
* ``compensation`` — the tool that undoes this one, or ``None`` when the effect is
  irreversible. Declaring ``None`` is a decision, not an omission: workflows order
  their steps so irreversible effects come last, and the engine can only enforce
  that if it knows which effects those are.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel

from harmony.kernel.registry import Registry

if TYPE_CHECKING:
    from harmony.identity.session import Session

TIn = TypeVar("TIn", bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)


class ToolCall(BaseModel):
    """A request to run one tool. Produced by planners and by workflow steps."""

    tool: str
    params: dict[str, Any] = {}
    step_id: str = ""
    """Stable within a run. Part of the idempotency key, so re-running the same
    logical step is a replay while a genuinely new call is not."""

    rationale: str = ""
    """Why this call, in the planner's words. Carried into the audit so a reader
    sees the agent's reason alongside the effect."""


class ToolResult(BaseModel):
    """The outcome of one invocation."""

    tool: str
    step_id: str = ""
    ok: bool
    output: dict[str, Any] = {}
    error: dict[str, Any] | None = None
    idempotency_key: str = ""
    replayed: bool = False
    """True when the result came from the idempotency store rather than a fresh
    execution. Visible in the audit: an auditor should be able to tell the
    difference between "we did it" and "we had already done it"."""


@dataclass(frozen=True)
class ToolSpec(Generic[TIn, TOut]):
    """The registered description of one tool."""

    name: str
    description: str
    input_model: type[TIn]
    output_model: type[TOut]
    scopes: frozenset[str]
    writes: bool
    compensation: str | None
    fn: Callable[["Session", TIn], TOut]
    system: str
    """Which system of record this touches; used for grouping in the catalog and
    for attributing effects in the audit narrative."""

    def json_schema(self) -> dict[str, Any]:
        """The input schema shown to the model."""
        return self.input_model.model_json_schema()

    def describe(self) -> dict[str, Any]:
        """A compact description for the planner's prompt."""
        return {
            "name": self.name,
            "system": self.system,
            "description": self.description,
            "writes": self.writes,
            "parameters": self.json_schema(),
        }


TOOLS: Registry[ToolSpec] = Registry("tool")


def tool(
    name: str,
    *,
    description: str,
    scopes: set[str] | frozenset[str],
    input: type[TIn],
    output: type[TOut],
    writes: bool = False,
    compensation: str | None = None,
    system: str | None = None,
) -> Callable[[Callable[["Session", TIn], TOut]], Callable[["Session", TIn], TOut]]:
    """Register a tool.

    The decorated function receives a :class:`Session` and a validated input model,
    and must return an instance of ``output``. It may assume authorisation has
    already been checked — that is the invoker's job, and doing it here as well
    would invite the two checks to drift apart.

        @tool("erp.cancel_purchase_order",
              description="Cancel an open purchase order.",
              scopes={"erp:po:cancel"},
              input=CancelPOInput, output=CancelPOOutput,
              writes=True, compensation="erp.restore_purchase_order")
        def cancel_purchase_order(session, inp): ...
    """
    if writes and not scopes:
        raise ValueError(f"tool '{name}' writes but declares no scopes")

    def decorator(fn: Callable[["Session", TIn], TOut]) -> Callable[["Session", TIn], TOut]:
        spec = ToolSpec(
            name=name,
            description=description,
            input_model=input,
            output_model=output,
            scopes=frozenset(scopes),
            writes=writes,
            compensation=compensation,
            fn=fn,
            system=system or name.split(".", 1)[0],
        )
        TOOLS.register(name, spec)
        fn.__tool_spec__ = spec  # type: ignore[attr-defined]
        return fn

    return decorator
