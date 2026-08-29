"""Infrastructure a tool or provider needs at call time.

Tools receive ``(session, input)`` and nothing else. That signature is worth
protecting — it is what makes a tool trivially testable and keeps the invoker's
contract with them simple — but a tool that writes to the ERP obviously needs a
database handle, and one that schedules a follow-up needs the queue.

The resolution is to hang the infrastructure off the session rather than widening
every tool signature or reaching for a module-level singleton. A session already
represents "the authorised context in which work happens"; the store and the queue
are part of that context.

Only *external-service handles* belong here: the database, the task queue, the
model client. Business services — the planner, the gate, the orchestrator — do not,
and that exclusion is load-bearing. A provider that could reach the gate could route
around it, and a tool that could reach the orchestrator could start a run that
nobody approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harmony.kernel.store import Store
    from harmony.llm.client import LLMClient
    from harmony.schedule.queue import TaskQueue


@dataclass(frozen=True)
class RuntimeServices:
    """Handles a tool or provider may use while executing."""

    store: "Store"
    tasks: "TaskQueue"
    llm: "LLMClient"
    """The model client, for providers that extract structure from prose. Passed
    rather than imported so that replay mode reaches extraction too — otherwise the
    demo would still hit the network for every email it reads."""
