"""The gate: run every rule, compose one decision, record all of it.

Composition is deliberately blunt:

* **Any deny wins.** A single rule objecting is enough, and no other rule can
  overrule it. There is no scoring, no weighting, and no way for two permissive
  rules to outvote a restrictive one.
* **Otherwise the strictest approval wins.** If several rules demand approval, the
  most senior demanded approver is the one asked. The others' demands are satisfied
  by definition, and all of them are recorded.
* **Otherwise allow.**

Every rule runs even after one has denied. Short-circuiting would be faster and
would make the audit worse: a reviewer asking "would this have needed a manager
anyway?" deserves an answer, and a run that denied for a missing scope should still
record that the value was also over the limit.

A rule that raises is treated as a denial. A policy check that cannot complete has
not passed, and failing open here would make every future bug in a rule a silent
authorisation.
"""

from __future__ import annotations

from collections.abc import Sequence

from harmony.audit.models import EventType
from harmony.gate.models import GateContext, GateDecision, RuleVerdict, Verdict
from harmony.gate.rules import GATE_RULES, GateRule
from harmony.identity.directory import most_senior


class Gate:
    """Evaluates a proposal against every registered rule."""

    def __init__(self, rules: Sequence[tuple[str, GateRule]] | None = None) -> None:
        self._rules = list(rules) if rules is not None else list(GATE_RULES)

    @property
    def rule_ids(self) -> list[str]:
        return [rule_id for rule_id, _ in self._rules]

    def evaluate(self, ctx: GateContext) -> GateDecision:
        """Run every rule and compose the decision."""
        verdicts = [self._run_rule(ctx, rule_id, rule) for rule_id, rule in self._rules]

        for verdict in verdicts:
            ctx.session.audit.emit(
                EventType.GATE_RULE_EVALUATED,
                f"[{verdict.rule_id}] {verdict.verdict.value}: {verdict.reason}",
                rule_id=verdict.rule_id,
                verdict=verdict.verdict.value,
                reason=verdict.reason,
                approver_id=verdict.approver_id,
                **verdict.details,
            )

        decision = self._compose(ctx, verdicts)
        ctx.session.audit.emit(
            EventType.GATE_EVALUATED,
            f"gate decision: {decision.verdict.value} — {decision.summary()}",
            verdict=decision.verdict.value,
            approver_id=decision.approver_id,
            rules_run=[v.rule_id for v in verdicts],
            denials=[v.rule_id for v in decision.denials()],
            proposal_digest=ctx.proposal.digest()[:12],
        )
        return decision

    @staticmethod
    def _run_rule(ctx: GateContext, rule_id: str, rule: GateRule) -> RuleVerdict:
        try:
            return rule(ctx)
        except Exception as exc:  # noqa: BLE001 - a broken rule must not authorise
            return RuleVerdict.deny(
                rule_id,
                f"rule raised and could not reach a verdict: {exc}",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    @staticmethod
    def _compose(ctx: GateContext, verdicts: list[RuleVerdict]) -> GateDecision:
        denials = [v for v in verdicts if v.verdict is Verdict.DENY]
        if denials:
            return GateDecision(
                verdict=Verdict.DENY,
                rule_verdicts=verdicts,
                reasons=[v.reason for v in denials],
            )

        approvals = [v for v in verdicts if v.verdict is Verdict.REQUIRE_APPROVAL]
        if approvals:
            candidates = [v.approver_id for v in approvals if v.approver_id]
            approver = (
                most_senior(ctx.directory, candidates)
                if candidates
                else ctx.session.principal.id
            )
            return GateDecision(
                verdict=Verdict.REQUIRE_APPROVAL,
                approver_id=approver,
                rule_verdicts=verdicts,
                reasons=[v.reason for v in approvals],
            )

        return GateDecision(verdict=Verdict.ALLOW, rule_verdicts=verdicts)
