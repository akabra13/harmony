"""Exception hierarchy for the harness.

Every error raised deliberately by the harness derives from :class:`HarmonyError`
and carries a stable ``code``. Codes appear in audit payloads, so they are part of
the observable contract: renaming one changes what an auditor reads.
"""

from __future__ import annotations

from typing import Any


class HarmonyError(Exception):
    """Base class for all deliberate harness failures."""

    code = "harmony_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


# --- authorization -------------------------------------------------------------


class ScopeDenied(HarmonyError):
    """The acting principal lacks a scope required to read or write."""

    code = "scope_denied"

    def __init__(self, *, principal_id: str, missing: frozenset[str], subject: str) -> None:
        super().__init__(
            f"{principal_id} lacks {sorted(missing)} required by {subject}",
            principal_id=principal_id,
            missing=sorted(missing),
            subject=subject,
        )
        self.missing = missing


# --- registries and catalogs ---------------------------------------------------


class NotRegistered(HarmonyError):
    """A name was referenced that no plugin has registered."""

    code = "not_registered"


class DuplicateRegistration(HarmonyError):
    """Two plugins claimed the same name."""

    code = "duplicate_registration"


# --- planning ------------------------------------------------------------------


class PlanRejected(HarmonyError):
    """The planner produced something the harness will not consider.

    Raised before the gate ever runs: an unknown tool name, a workflow that is not
    bound to the profile, parameters that fail their declared schema.
    """

    code = "plan_rejected"


# --- gating --------------------------------------------------------------------


class ApprovalRequired(HarmonyError):
    """Execution was attempted on a proposal that has not been approved."""

    code = "approval_required"


class ApprovalMismatch(HarmonyError):
    """The approved proposal digest does not match the proposal being executed."""

    code = "approval_mismatch"


# --- execution -----------------------------------------------------------------


class ToolFailed(HarmonyError):
    """A tool raised while executing. Triggers compensation upstream."""

    code = "tool_failed"


class CompensationFailed(HarmonyError):
    """A compensating action itself failed, leaving partial effects in place."""

    code = "compensation_failed"


# --- workflow ------------------------------------------------------------------


class WorkflowDefinitionInvalid(HarmonyError):
    """A workflow definition failed validation at load time."""

    code = "workflow_definition_invalid"


class WorkflowStepFailed(HarmonyError):
    """A step inside a workflow instance failed."""

    code = "workflow_step_failed"


class BindingUnresolved(HarmonyError):
    """A ``${...}`` binding expression could not be resolved."""

    code = "binding_unresolved"


# --- llm -----------------------------------------------------------------------


class LLMOutputInvalid(HarmonyError):
    """The model returned something outside the bounds declared for the call site."""

    code = "llm_output_invalid"


class CassetteMiss(HarmonyError):
    """Replay mode was asked for a prompt that was never recorded."""

    code = "cassette_miss"
