"""Working memory: what one run knows while it is running.

The distinction this module exists to draw is between what a run *knows* and what
the organisation *believes*. Working memory is the former: the attention item, the
context bundle, the step outputs, the running notes. It lives for the length of a
run and is then discarded, except for whatever the run explicitly promotes into
durable memory.

Keeping it a real object rather than a pile of local variables buys two things: the
planner receives one thing instead of six, and a resumed run can reconstruct its
knowledge from persisted state rather than re-deriving it from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harmony.detect.models import AttentionItem
from harmony.providers.base import ContextBundle


@dataclass
class WorkingMemory:
    """Everything the current run has learned."""

    item: AttentionItem
    context: ContextBundle = field(default_factory=ContextBundle)
    recalled: list[dict[str, Any]] = field(default_factory=list)
    """Durable facts pulled in for this run. Advisory only — see
    :mod:`harmony.memory.durable` for why that constraint matters."""

    notes: list[str] = field(default_factory=list)
    step_outputs: dict[str, Any] = field(default_factory=dict)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def for_prompt(self) -> dict[str, Any]:
        """The knowledge the planner is shown.

        Recalled beliefs are labelled as such and carry their confidence, so the
        model can weigh "Meridian has been reliable" differently from "the ERP says
        on_hand is 150". Presenting them identically would let a soft belief harden
        into a hard fact somewhere in the model's attention.
        """
        return {
            "attention_item": self.item.describe(),
            "context": {
                slice_.system: slice_.collections for slice_ in self.context.slices
            },
            "systems_not_readable": self.context.unreachable,
            "redactions": [
                {
                    "system": slice_.system,
                    "collection": r.collection,
                    "count": r.count,
                    "reason": r.reason,
                }
                for slice_ in self.context.slices
                for r in slice_.redactions
            ],
            "remembered_beliefs": self.recalled,
        }
