"""The bounded model call.

Every LLM call in Harmony goes through :func:`ask`. That is what makes the LLM
boundary table in the README a fact about the code rather than a description of
intent: there is one function, it always takes a pydantic model describing the only
acceptable answer, and it always audits what was asked and what came back.

Bounding happens in three layers, cheapest first:

1. **Schema** — the model is given the answer's JSON Schema and forced to use it,
   so a wrong *shape* is usually impossible rather than merely detected.
2. **Validation** — pydantic parses the reply. Types, required fields, enum
   membership and length limits are enforced here.
3. **Guardrails** — callables that check things a schema cannot express: that a
   drafted notification mentions the production order it is about, that a chosen
   supplier came from the candidate list a prior deterministic step computed.

A rejection is audited with the offending output before the retry, so
``LLM_OUTPUT_REJECTED`` in the ledger is a complete record of the model trying
something it was not allowed to do. Then it fails closed. The harness never falls
back to "use the model's answer anyway".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from harmony.audit.models import EventType
from harmony.identity.session import Session
from harmony.kernel.errors import LLMOutputInvalid
from harmony.kernel.ids import short_digest
from harmony.llm.client import LLMClient, LLMRequest

T = TypeVar("T", bound=BaseModel)

Guardrail = Callable[[BaseModel], None]
"""A check that raises :class:`LLMOutputInvalid` when the output is unacceptable."""


def ask(
    client: LLMClient,
    session: Session,
    *,
    call_site: str,
    system: str,
    prompt: str,
    output_model: type[T],
    guardrails: Sequence[Guardrail] = (),
    max_tokens: int = 2048,
    retries: int = 1,
) -> T:
    """Ask the model for a structured answer, and accept nothing else."""
    request = LLMRequest(
        call_site=call_site,
        system=system,
        prompt=prompt,
        output_schema=output_model.model_json_schema(),
        max_tokens=max_tokens,
    )

    last_error: str = ""
    for attempt in range(retries + 1):
        attempt_request = (
            request
            if attempt == 0
            else request.model_copy(
                update={
                    "prompt": (
                        f"{request.prompt}\n\n"
                        f"Your previous answer was rejected: {last_error}\n"
                        "Answer again, satisfying every constraint."
                    )
                }
            )
        )

        response = client.complete_structured(attempt_request)
        session.audit.emit(
            EventType.LLM_CALLED,
            f"asked the model: {call_site}",
            call_site=call_site,
            attempt=attempt + 1,
            prompt_digest=short_digest(attempt_request.prompt),
            prompt_chars=len(attempt_request.prompt),
            output_schema_digest=short_digest(request.output_schema),
            **response.usage(),
        )

        try:
            parsed = output_model.model_validate(response.output)
        except ValidationError as exc:
            last_error = _describe_validation_error(exc)
            _audit_rejection(session, call_site, attempt, last_error, response.output)
            continue

        try:
            for guardrail in guardrails:
                guardrail(parsed)
        except LLMOutputInvalid as exc:
            last_error = exc.message
            _audit_rejection(session, call_site, attempt, last_error, response.output)
            continue

        return parsed

    raise LLMOutputInvalid(
        f"model failed to produce an acceptable answer for '{call_site}' "
        f"after {retries + 1} attempt(s): {last_error}",
        call_site=call_site,
        last_error=last_error,
    )


def _audit_rejection(
    session: Session, call_site: str, attempt: int, error: str, output: dict
) -> None:
    session.audit.emit(
        EventType.LLM_OUTPUT_REJECTED,
        f"rejected the model's answer for {call_site}: {error}",
        call_site=call_site,
        attempt=attempt + 1,
        error=error,
        rejected_output=output,
    )


def _describe_validation_error(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
        for err in exc.errors(include_url=False)
    ]
    return "; ".join(parts[:5])


# --- guardrail constructors ----------------------------------------------------
#
# Named factories rather than inline lambdas so that a rejection reads well in the
# audit: "must mention 4812" is a sentence a reviewer can act on.


def must_mention(*required: str, field: str = "body") -> Guardrail:
    """The named text field must contain every required string.

    Used on drafted notifications: a message about production order 4812 that never
    says "4812" is not a notification, it is a rumour.
    """

    def check(output: BaseModel) -> None:
        text = str(getattr(output, field, "") or "")
        missing = [token for token in required if token and token not in text]
        if missing:
            raise LLMOutputInvalid(
                f"{field} must mention {missing}", field=field, missing=missing
            )

    return check


def must_choose_from(allowed: Sequence[str], *, field: str) -> Guardrail:
    """The named field must be one of a pre-computed set.

    The set comes from a deterministic step that ran first — approved suppliers
    filtered by lead time, say. This is the constraint that keeps an LLM step
    inside a declared workflow from becoming a decision the workflow did not
    authorise: the model chooses *among* options that code produced, and its
    reasoning is recorded as a justification rather than trusted as a filter.
    """
    allowed_set = set(allowed)

    def check(output: BaseModel) -> None:
        value = getattr(output, field, None)
        if value not in allowed_set:
            raise LLMOutputInvalid(
                f"{field}={value!r} is not among the permitted choices {sorted(allowed_set)}",
                field=field,
                value=value,
                allowed=sorted(allowed_set),
            )

    return check


def max_length(limit: int, *, field: str) -> Guardrail:
    """The named text field must not exceed ``limit`` characters."""

    def check(output: BaseModel) -> None:
        text = str(getattr(output, field, "") or "")
        if len(text) > limit:
            raise LLMOutputInvalid(
                f"{field} is {len(text)} characters; the limit is {limit}",
                field=field,
                length=len(text),
                limit=limit,
            )

    return check
