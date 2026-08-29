"""Gate rules: independent checks, composed by the pipeline.

A rule looks at a :class:`GateContext` and returns one :class:`RuleVerdict`. It does
not know about other rules, it cannot see their answers, and it never decides the
outcome — composition is the pipeline's job. That independence is what makes the
gate extensible without becoming a tangle: adding a policy means adding a rule, and
the blast radius of getting one wrong is one verdict in the ledger.

Two rules ship with the kernel because they are true for any customer:

``scope``
    The acting principal must hold every scope the plan's tools require.

``human_approval_for_writes``
    Any plan that writes needs a named human to agree to it.

Everything else is a policy of a particular company, and lives in
``northfield/rules.py``: what a purchase order may be worth before a manager must
sign, which suppliers are usable for which parts. Those belong to the business, not
to the harness, and putting them here would be the first step towards a harness
that only works for one manufacturer.
"""

from __future__ import annotations

from collections.abc import Callable

from harmony.gate.models import GateContext, RuleVerdict
from harmony.kernel.registry import Registry

GateRule = Callable[[GateContext], RuleVerdict]

GATE_RULES: Registry[GateRule] = Registry("gate rule")


def gate_rule(rule_id: str) -> Callable[[GateRule], GateRule]:
    """Register a gate rule.

        @gate_rule("po_value_threshold")
        def check(ctx: GateContext) -> RuleVerdict: ...

    Return :meth:`RuleVerdict.allow` when the rule has no objection — including when
    it does not apply. A rule that returns nothing tells the audit nothing, and
    "this rule considered the plan and was content" is worth recording.
    """

    def decorator(fn: GateRule) -> GateRule:
        GATE_RULES.register(rule_id, fn)
        return fn

    return decorator


# --- kernel rules --------------------------------------------------------------


@gate_rule("scope")
def scope_rule(ctx: GateContext) -> RuleVerdict:
    """The principal must hold every scope the plan's tools require.

    The tool invoker checks this again per call, and that repetition is deliberate.
    Checking here means an unauthorised plan is refused *whole*, before anything
    runs, so the human sees a clean denial rather than a plan that fails partway and
    leaves the first two steps done.
    """
    required = ctx.required_scopes
    missing = ctx.session.scopes.missing(required)
    if missing:
        return RuleVerdict.deny(
            "scope",
            f"{ctx.session.principal.label} lacks {sorted(missing)}",
            required=sorted(required),
            missing=sorted(missing),
            held=sorted(ctx.session.scopes),
            tools=[t.name for t in ctx.tools],
        )
    return RuleVerdict.allow(
        "scope",
        f"holds all {len(required)} required scope(s)",
        required=sorted(required),
    )


@gate_rule("human_approval_for_writes")
def human_approval_rule(ctx: GateContext) -> RuleVerdict:
    """Nothing writes without a human agreeing to this specific plan.

    The default approver is the principal the agent acts for: it is their work, and
    the agent is their instrument. Company rules may escalate above them — see
    ``northfield/rules.py`` — but nothing lowers the bar below "someone said yes".
    """
    writes = ctx.write_tools
    if not writes:
        return RuleVerdict.allow(
            "human_approval_for_writes", "plan makes no changes to any system"
        )
    return RuleVerdict.approval(
        "human_approval_for_writes",
        f"plan writes to {sorted({t.system for t in writes})}; a human must agree",
        approver_id=ctx.session.principal.id,
        write_tools=[t.name for t in writes],
        systems=sorted({t.system for t in writes}),
    )
