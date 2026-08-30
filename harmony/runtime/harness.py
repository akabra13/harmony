"""The composition root.

Every dependency in the harness is constructed exactly once, here, and passed
explicitly. There are no module-level singletons holding a database handle, no
service locators, and nothing that reaches for a global clock — which is what makes
a test able to build a whole harness against an in-memory database at an arbitrary
simulated date, and what makes "run the same code with a different company" a real
possibility rather than an aspiration.

:meth:`Harness.build` is deliberately readable top to bottom: storage, then time,
then identity, then the eight responsibilities in the order the loop uses them.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import yaml

from harmony.audit.log import AuditLog, AuditWriter
from harmony.detect.base import DETECTORS
from harmony.detect.dedupe import AttentionItemStore
from harmony.gate.approvals import ApprovalService
from harmony.gate.pipeline import Gate
from harmony.identity.models import PrincipalKind
from harmony.identity.session import Session, system_session
from harmony.kernel.clock import SimulatedClock, parse_datetime
from harmony.kernel.migrations import HARMONY_MIGRATIONS
from harmony.kernel.registry import load_plugin_modules
from harmony.kernel.services import RuntimeServices
from harmony.kernel.store import Store
from harmony.llm.client import LLMClient
from harmony.llm.replay import build_client
from harmony.memory.durable import MemoryStore
from harmony.plan.planner import Planner
from harmony.plan.repository import ProposalRepository
from harmony.providers.broker import ContextBroker
from harmony.runtime.deployment import Deployment
from harmony.runtime.profile import load_profiles
from harmony.runtime.run import RunRepository
from harmony.schedule.queue import TaskQueue
from harmony.tools.catalog import ToolCatalog
from harmony.tools.invoker import ToolInvoker
from harmony.workflow.engine import WorkflowEngine
from harmony.workflow.loader import load_directory

DEFAULT_DB = Path(".harmony/harmony.db")


class Harness:
    """Everything, wired together."""

    def __init__(
        self,
        *,
        deployment: Deployment,
        store: Store,
        clock: SimulatedClock,
        llm: LLMClient,
    ) -> None:
        self.deployment = deployment
        self.store = store
        self.clock = clock
        self.llm = llm

        # --- cross-cutting -----------------------------------------------------
        self.audit_log = AuditLog(store, clock)
        self.directory = deployment.directory_factory(store)

        # --- catalogs, populated by the company's plugin imports ---------------
        self.tools = ToolCatalog()
        self.detectors = DETECTORS
        self.workflows = load_directory(deployment.workflows_dir, catalog=self.tools)
        self.profiles = load_profiles(deployment.profiles_dir)
        self.policy: dict[str, Any] = (
            yaml.safe_load(Path(deployment.policy_path).read_text(encoding="utf-8")) or {}
            if Path(deployment.policy_path).exists()
            else {}
        )

        # --- the eight responsibilities, in loop order -------------------------
        self.items = AttentionItemStore(store)
        self.broker = ContextBroker()
        self.memory = MemoryStore(store)
        self.planner = Planner(llm=llm, tools=self.tools, workflows=self.workflows)
        self.gate = Gate()
        self.invoker = ToolInvoker(store, self.tools)
        self.engine = WorkflowEngine(
            store=store, catalog=self.workflows, invoker=self.invoker, llm=llm
        )
        self.tasks = TaskQueue(store)
        self.approvals = ApprovalService(
            store=store,
            tasks=self.tasks,
            directory=self.directory,
            availability=deployment.availability_factory(self),
        )
        self.runs = RunRepository(store)
        self.proposals = ProposalRepository(store)

        # Infrastructure handed to tools and providers via the session.
        self.services = RuntimeServices(store=store, tasks=self.tasks, llm=llm)

    # --- construction ----------------------------------------------------------

    @classmethod
    def build(
        cls,
        deployment: Deployment,
        *,
        db_path: Path | str = DEFAULT_DB,
        start_at: str | _dt.datetime | None = None,
        llm: LLMClient | None = None,
        seed: bool = True,
    ) -> "Harness":
        """Construct a harness for one deployment.

        Importing the company's plugin package is the first substantive step: the
        tool catalog, provider registry and detector registry are all populated as
        an import side effect, and the workflow loader validates against a catalog
        that must already be full.
        """
        # Registering the harness's own plugins before the company's: the
        # scheduling tools and the two task handlers ship with the kernel, and a
        # company workflow may reference them.
        import harmony.schedule.tools  # noqa: F401  (registers schedule.* tools)
        import harmony.runtime.handlers  # noqa: F401  (registers task handlers)

        load_plugin_modules(deployment.plugin_package)

        store = Store(db_path)
        store.migrate(HARMONY_MIGRATIONS)
        store.migrate(deployment.migrations)

        clock = _load_or_start_clock(store, start_at)
        if seed:
            deployment.seed(store)

        harness = cls(
            deployment=deployment,
            store=store,
            clock=clock,
            llm=llm or build_client(),
        )
        harness._validate_startup()
        return harness

    def _validate_startup(self) -> None:
        """Fail loudly at start-up rather than quietly at run time.

        Everything checked here is a wiring mistake that would otherwise surface
        mid-run: a compensation naming a tool that does not exist, a profile
        referencing an unregistered detector, a workflow a profile binds that was
        never loaded, or a profile advertising a tool it has not asked for the
        scope of — which would look available to the planner and then be refused
        by the invoker, wasting a model call to reach a foregone conclusion.
        """
        problems = list(self.tools.validate_references())

        # An empty directory means the database has not been populated yet, not
        # that the profiles are wrong. Checking assignments against nobody would
        # turn "you have not run `harmony init`" into four confusing errors about
        # unknown users.
        directory_populated = bool(self.directory.all())

        for profile in self.profiles.all():
            for detector_id in profile.detectors:
                if detector_id not in self.detectors:
                    problems.append(
                        f"profile '{profile.id}' lists detector '{detector_id}', "
                        "which is not registered"
                    )
            for pattern in profile.tools:
                if not self.tools.matching([pattern]):
                    problems.append(
                        f"profile '{profile.id}' offers tool pattern '{pattern}', "
                        "which matches no registered tool"
                    )

            declared = profile.scope_set()
            if declared is not None:
                for spec in self.tools.matching(profile.tools):
                    missing = spec.scopes - declared
                    if missing:
                        problems.append(
                            f"profile '{profile.id}' offers '{spec.name}' but does not "
                            f"declare {sorted(missing)}, so it could never be invoked"
                        )

            for workflow_name in profile.workflows:
                if not self.workflows.has(workflow_name):
                    problems.append(
                        f"profile '{profile.id}' binds workflow '{workflow_name}', "
                        "which is not loaded"
                    )
            if directory_populated:
                for user_id in profile.assigned_to:
                    if self.directory.try_get(user_id) is None:
                        problems.append(
                            f"profile '{profile.id}' is assigned to unknown user '{user_id}'"
                        )

        if problems:
            raise RuntimeError(
                "harness failed start-up validation:\n  - " + "\n  - ".join(problems)
            )

    # --- sessions --------------------------------------------------------------

    def user_session(
        self,
        user_id: str,
        *,
        run_id: str,
        profile_scopes: frozenset[str] | None = None,
        purpose: str = "",
    ) -> Session:
        """Mint a session acting as a named employee."""
        principal = self.directory.get(user_id)
        return Session.issue(
            principal=principal,
            run_id=run_id,
            audit=self.root_audit(),
            clock=self.clock,
            profile_scopes=profile_scopes,
            purpose=purpose,
            services=self.services,
        )

    def system_session(self, *, run_id: str, purpose: str) -> Session:
        """Mint the narrow non-human session detectors and escalations run under."""
        return system_session(
            principal_id=self.deployment.system_principal_id,
            scopes=self.deployment.system_scopes,
            run_id=run_id,
            audit=self.root_audit(),
            clock=self.clock,
            purpose=purpose,
            services=self.services,
        )

    def root_audit(self) -> AuditWriter:
        return AuditWriter(
            self.audit_log,
            run_id=None,
            actor_kind=PrincipalKind.SYSTEM.value,
            actor_id=self.deployment.system_principal_id,
        )

    def system_audit(self, *, run_id: str | None = None) -> AuditWriter:
        return self.root_audit().bind(run_id=run_id)

    # --- clock -----------------------------------------------------------------

    def advance_clock(self, target: str | _dt.date | _dt.datetime) -> _dt.datetime:
        """Move simulated time forward and persist the new position."""
        if isinstance(target, str):
            target = parse_datetime(target)
        before = self.clock.now()
        after = self.clock.advance_to(target)
        _persist_clock(self.store, after)
        from harmony.audit.models import EventType

        self.root_audit().emit(
            EventType.CLOCK_ADVANCED,
            f"clock advanced from {before.isoformat()} to {after.isoformat()}",
            from_time=before.isoformat(),
            to_time=after.isoformat(),
        )
        return after

    def close(self) -> None:
        self.store.close()


# --- clock persistence ---------------------------------------------------------


def _load_or_start_clock(
    store: Store, start_at: str | _dt.datetime | None
) -> SimulatedClock:
    """Resume the simulated clock where it was, or start it fresh.

    Persisting the clock is what makes a restart continue rather than travel back
    in time — which would make already-fired scheduled tasks come due again.
    """
    row = store.query_one("SELECT now FROM clock_state WHERE id = 1")
    if row and start_at is None:
        return SimulatedClock(
            _dt.datetime.fromisoformat(row["now"]),
            on_change=lambda now: _persist_clock(store, now),
        )

    start = parse_datetime(start_at) if start_at else _dt.datetime(2026, 9, 2, 8, 0, 0)
    _persist_clock(store, start)
    return SimulatedClock(start, on_change=lambda now: _persist_clock(store, now))


def _persist_clock(store: Store, now: _dt.datetime) -> None:
    store.execute(
        "INSERT INTO clock_state (id, now) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET now = excluded.now",
        (now.isoformat(),),
    )
