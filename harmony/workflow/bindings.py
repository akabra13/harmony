"""Binding expressions: ``${params.x}``, ``${steps.y.output.z}``, ``${clock.today}``.

Sixty lines, three namespaces, no ``eval``. This is the whole expression language,
and keeping it this small was a deliberate decision rather than a shortcut.

The pull towards an expression language here is strong — conditionals, arithmetic,
string formatting, filters. Every one of those moves business logic out of code
that can be tested and typed and into strings that cannot. The line drawn is:
*bindings move values, code computes them.* If a workflow needs a derived value, a
tool computes it and a later step reads its output. That is why
``erp.filter_suppliers_by_lead_time`` exists as a tool rather than as a
``when:`` clause.

A binding that does not resolve is an error, never an empty string. Silent
substitution of nothing is how a purchase order gets created for quantity zero.
"""

from __future__ import annotations

import re
from typing import Any

from harmony.kernel.clock import Clock
from harmony.kernel.errors import BindingUnresolved

BINDING = re.compile(r"^\$\{([^}]+)\}$")
EMBEDDED = re.compile(r"\$\{([^}]+)\}")


class BindingContext:
    """The three namespaces a binding may read."""

    def __init__(
        self,
        *,
        params: dict[str, Any],
        step_results: dict[str, Any],
        clock: Clock,
    ) -> None:
        self.params = params
        self.step_results = step_results
        self.clock = clock

    def resolve_path(self, path: str) -> Any:
        """Resolve one dotted path. Raises when any segment is missing."""
        head, _, rest = path.partition(".")

        if head == "params":
            return self._walk(self.params, rest, path)
        if head == "steps":
            return self._walk(self.step_results, rest, path)
        if head == "clock":
            return self._clock_value(rest, path)
        raise BindingUnresolved(
            f"unknown binding namespace '{head}' in '${{{path}}}'; "
            "expected params, steps or clock",
            path=path,
        )

    def _clock_value(self, rest: str, path: str) -> Any:
        if rest == "today":
            return self.clock.today()
        if rest == "now":
            return self.clock.now()
        raise BindingUnresolved(
            f"unknown clock field '{rest}' in '${{{path}}}'; expected today or now",
            path=path,
        )

    @staticmethod
    def _walk(root: Any, rest: str, path: str) -> Any:
        current = root
        for segment in filter(None, rest.split(".")):
            if isinstance(current, dict) and segment in current:
                current = current[segment]
            elif isinstance(current, list) and segment.isdigit():
                current = current[int(segment)]
            else:
                raise BindingUnresolved(
                    f"'${{{path}}}' could not be resolved: no '{segment}' at that point",
                    path=path,
                    segment=segment,
                    available=sorted(current) if isinstance(current, dict) else None,
                )
        return current


def resolve(value: Any, ctx: BindingContext) -> Any:
    """Recursively resolve bindings in a value of any shape.

    A string that is *entirely* one binding keeps the resolved value's type — so
    ``"${params.qty}"`` yields the integer 400, not the string "400". A binding
    embedded in surrounding text is interpolated as text.
    """
    if isinstance(value, str):
        whole = BINDING.match(value.strip())
        if whole:
            return ctx.resolve_path(whole.group(1).strip())
        if "${" in value:
            return EMBEDDED.sub(
                lambda m: str(ctx.resolve_path(m.group(1).strip())), value
            )
        return value
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    return value


def referenced_paths(value: Any) -> set[str]:
    """Every binding path a value mentions. Used by load-time validation."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(m.strip() for m in EMBEDDED.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            found |= referenced_paths(item)
    elif isinstance(value, list):
        for item in value:
            found |= referenced_paths(item)
    return found
