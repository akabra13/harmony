# Graph Report - .  (2026-08-29)

## Corpus Check
- 117 files · ~51,550 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1319 nodes · 3473 edges · 83 communities (80 shown, 3 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 422 edges (avg confidence: 0.6)
- Token cost: 34,000 input · 11,500 output

## Community Hubs (Navigation)
- Tool Definitions & Sessions
- LLM Client & Replay
- Gate Rules & Decisions
- Audit Log, IDs & Durable Memory
- Profile & Plugin Registry
- Principals & User Directory
- Detector Framework
- CLI Command Surface
- Context Provider Framework
- Tool Invocation & Execution Grants
- Workflow Engine Execution
- Run Lifecycle & Audit Events
- Plan Models & Repository
- Orchestrator Loop
- Approval Service & Escalation
- Attention Item Dedupe
- Workflow Definition Models
- Hash-Chained Audit Log
- Tool Catalog & Compensation Checks
- ERP System Of Record
- Durable Task Queue
- Workflow Loading & Validation
- Demo Scenario Presentation
- Mail Commitment Extraction
- Quality Lots & Shortage Flags
- Planner & Working Memory
- SQLite Store & Transactions
- Run Explanation
- Binding Expression Resolution
- Simulated Clock
- Workflow Instance Repository
- Harness Bootstrap & Migrations
- Gate Vocabulary & Northfield Rules
- Mail System & Inbox Filtering
- Parts, Orders & Supply Seed Data
- Approval State & Availability
- Context Bundle & Scanning
- Error Hierarchy & Scope Denial
- Run Repository & State
- Harness Sessions & Startup
- Calendar Availability
- Runtime Services & Deployment
- Scheduled Work Vocabulary
- Context Broker
- Free-Form Plan Executor
- Scenario B Quality Hold Cast
- Clock & Worker CLI
- Worker Tick & Follow-Ups
- Calendar Provider & System Principal
- Init & Audit Explain CLI
- Tool Schema Description
- Binding & Planner Descriptions
- Lot Hold Detection Noise
- Shortfall Projection
- Scenario A CLI Entry
- Northwind Materials (isolated supplier)
- Project Manifest

## God Nodes (most connected - your core abstractions)
1. `Session` - 169 edges
2. `Store` - 110 edges
3. `EventType` - 52 edges
4. `ToolFailed` - 45 edges
5. `Harness` - 44 edges
6. `Registry` - 40 edges
7. `Orchestrator` - 40 edges
8. `ExecutionGrant` - 37 edges
9. `ToolCatalog` - 31 edges
10. `HarmonyError` - 30 edges

## Surprising Connections (you probably didn't know these)
- `Profile Scope Intersection` --rationale_for--> `ExecutionGrant`  [INFERRED]
  northfield/profiles/purchasing_manager.yaml → harmony/identity/grant.py
- `Approval Policy` --conceptually_related_to--> `ApprovalService`  [INFERRED]
  northfield/policy.yaml → harmony/gate/approvals.py
- `Dana Whitfield - Purchasing Manager` --shares_data_with--> `UserDirectory`  [INFERRED]
  northfield/seed/users.yaml → harmony/identity/directory.py
- `Escalate To Manager` --references--> `manager_chain()`  [INFERRED]
  northfield/policy.yaml → harmony/identity/directory.py
- `Dana Whitfield - Purchasing Manager` --shares_data_with--> `manager_chain()`  [INFERRED]
  northfield/seed/users.yaml → harmony/identity/directory.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **PO Reroute Fixed Step Sequence** — northfield_workflows_po_reroute_v3_find_approved_suppliers, northfield_workflows_po_reroute_v3_filter_by_lead_time, northfield_workflows_po_reroute_v3_choose_supplier, northfield_workflows_po_reroute_v3_create_replacement_po, northfield_workflows_po_reroute_v3_reduce_original_po, northfield_workflows_po_reroute_v3_draft_notification, northfield_workflows_po_reroute_v3_notify_production, northfield_workflows_po_reroute_v3_schedule_arrival_check [EXTRACTED 1.00]
- **Scenario A: Shortfall To Reroute** — northfield_seed_messages_m_001, northfield_seed_purchase_orders_po_77812, northfield_seed_parts_p_4471, northfield_seed_production_orders_po_4812, northfield_seed_suppliers_s_y, northfield_seed_suppliers_s_z, northfield_detectors_material_shortfall_detect [INFERRED 0.95]
- **Scenario B: Lot Hold To Reallocation** — northfield_seed_messages_m_006, northfield_seed_quality_lots_l_2093, northfield_seed_quality_lots_l_2101, northfield_seed_parts_p_1188, northfield_seed_production_orders_po_4820, northfield_seed_users_u_202, northfield_detectors_lot_hold_allocation_risk_detect [INFERRED 0.95]

## Communities (83 total, 3 thin omitted)

### Community 0 - "Tool Definitions & Sessions"
Cohesion: 0.06
Nodes (84): Non-raising, non-auditing check. For deciding what to *offer*, never for, A strictly narrower session. Used when handing control to a subsystem         t, An authorised, audited, time-aware context for one run., The database handle, for tools and providers. Raises when a session was, Session, A tool raised while executing. Triggers compensation upstream., ToolFailed, new_id() (+76 more)

### Community 1 - "LLM Client & Replay"
Cohesion: 0.05
Nodes (63): Guardrail, CassetteMiss, LLMOutputInvalid, Exception hierarchy for the harness.  Every error raised deliberately by the har, The model returned something outside the bounds declared for the call site., Replay mode was asked for a prompt that was never recorded., AnthropicClient, Anthropic-backed structured completion.  Structured output is obtained by giving (+55 more)

### Community 2 - "Gate Rules & Decisions"
Cohesion: 0.05
Nodes (44): GateContext, GateDecision, Any, BaseModel, Parameters the proposal supplies to a named tool.          Only meaningful on th, The composed outcome of every rule., One rule's answer, with the reasoning that produced it., Everything a rule may consider.      Passed whole to every rule so that adding a (+36 more)

### Community 3 - "Audit Log, IDs & Durable Memory"
Cohesion: 0.06
Nodes (34): Append-only, hash-chained audit log.  Append-only is enforced three ways, in inc, canonical_json(), digest(), _normalise(), Any, Identifier and digest helpers.  Two rules shape this module:  * **Ids are prefix, Deterministic JSON: sorted keys, no incidental whitespace, stable separators., A stable hex digest over any number of structured parts. (+26 more)

### Community 4 - "Profile & Plugin Registry"
Cohesion: 0.07
Nodes (26): DuplicateRegistration, NotRegistered, A name was referenced that no plugin has registered., Two plugins claimed the same name., T, A tiny name-keyed plugin registry.  Detectors, providers, tools and gate rules a, Name → entry, with a helpful error when a name is missing., Used by tests that register throwaway plugins. (+18 more)

### Community 5 - "Principals & User Directory"
Cohesion: 0.07
Nodes (26): ApprovalLimits, Principal, PrincipalKind, BaseModel, StrEnum, Who is acting, and what they are entitled to do.  Scopes are opaque strings to t, Who the harness is acting as., A set of scope strings with a few domain-free conveniences. (+18 more)

### Community 6 - "Detector Framework"
Cohesion: 0.11
Nodes (30): detector(), DetectorContext, Detector registration and the context a detector runs in.  A detector is the onl, Register a detector.          @detector("material_shortfall",, What a detector is given: a scoped reader, a clock, and its arguments., Evidence, datetime, Attention items: the unit of "something a human should know about".  An item is (+22 more)

### Community 7 - "CLI Command Surface"
Cohesion: 0.07
Nodes (37): approvals_approve(), approvals_list(), approvals_reject(), approvals_show(), audit_runs(), audit_verify(), catalog_detectors(), catalog_profiles() (+29 more)

### Community 8 - "Context Provider Framework"
Cohesion: 0.11
Nodes (28): ContextProvider, ContextRequest, ContextSlice, provider(), ProviderSpec, BaseModel, Protocol, Context providers: one per system, each answerable for what it did and did not s (+20 more)

### Community 9 - "Tool Invocation & Execution Grants"
Cohesion: 0.12
Nodes (23): The free-form executor: running a plan the model assembled.  This is Scenario B', ExecutionGrant, Execution grants: the artifact a human approval produces.  Scopes answer "is thi, Authority to perform a specific, already-approved set of writes., ApprovalRequired, PlanRejected, The planner produced something the harness will not consider.      Raised before, Execution was attempted on a proposal that has not been approved. (+15 more)

### Community 10 - "Workflow Engine Execution"
Cohesion: 0.10
Nodes (19): CompensationFailed, A step inside a workflow instance failed., A compensating action itself failed, leaving partial effects in place., WorkflowStepFailed, _as_list(), Any, Continue an instance from wherever it stopped., Enforce ``on_empty: fail``.          This is how a workflow states a preconditio (+11 more)

### Community 11 - "Run Lifecycle & Audit Events"
Cohesion: 0.11
Nodes (21): EventType, StrEnum, The audit vocabulary.  Requirement 7 of the brief: *"from the audit log alone, s, DedupeResult, Whether this detection warrants a pass of the agent loop., The scoped session: the harness's stand-in for a downscoped access token.  Not, Working memory: what one run knows while it is running.  The distinction this mo, The planner: attention item plus context, in; a typed proposal, out.  This is th (+13 more)

### Community 12 - "Plan Models & Repository"
Cohesion: 0.11
Nodes (18): ActionKind, EvidenceRef, NoAction, PlannedCall, Proposal, Any, BaseModel, StrEnum (+10 more)

### Community 13 - "Orchestrator Loop"
Cohesion: 0.14
Nodes (15): Orchestrator, Any, Exception, Detect, then open a run for anything that turned out to be news., Take one attention item from detection to a decision., Plan, persist, and stop early if the planner declined to act., Record an approval and execute the plan it authorises., Record a rejection. Nothing is executed. (+7 more)

### Community 14 - "Approval Service & Escalation"
Cohesion: 0.15
Nodes (14): ApprovalRequest, ApprovalService, Any, BaseModel, Creates, routes, escalates and resolves approval requests., Ask a human, and schedule the check for what happens if they do not answer., Enqueue the end-of-day check.          The dedupe key includes the escalation co, Record a human's decision. (+6 more)

### Community 15 - "Attention Item Dedupe"
Cohesion: 0.12
Nodes (14): AttentionItemStore, DedupeOutcome, datetime, StrEnum, Dedupe: deciding whether a detection is news.  Detectors are pure — they look at, Persistence and dedupe for attention items., Decide what to do with a fresh detection, and record the decision., AttentionItem (+6 more)

### Community 16 - "Workflow Definition Models"
Cohesion: 0.15
Nodes (20): A workflow definition failed validation at load time., WorkflowDefinitionInvalid, The workflow interpreter.  The engine has no opinion about purchasing. It advanc, Loading and validating workflow definitions.  Every check in this module exists, Compensation, FieldSpec, InstanceStatus, OnEmpty (+12 more)

### Community 17 - "Hash-Chained Audit Log"
Cohesion: 0.11
Nodes (12): AuditLog, AuditWriter, Any, Recompute every hash. Returns ``(ok, first_broken_event_id)``., A run-scoped, actor-bound view of the log.      Handed to sessions, providers an, A writer for the same log with a different run or actor., Writes and reads the ledger. Never updates it., Append one event and return it, with its chain position filled in. (+4 more)

### Community 18 - "Tool Catalog & Compensation Checks"
Cohesion: 0.10
Nodes (14): A view over the global tool registry., Tools whose names match any glob pattern, e.g. ``["erp.*", "mail.send"]``., Tools this principal both holds the scopes for and is allowed by profile., The catalog as the planner sees it., Every declared compensation must name a tool that exists.          Called at sta, ToolCatalog, _check_compensation(), _check_llm_step() (+6 more)

### Community 19 - "ERP System Of Record"
Cohesion: 0.19
Nodes (24): create_purchase_order(), effective_arrival(), get_production_order(), get_purchase_order(), get_supplier(), list_parts(), list_purchase_orders(), list_suppliers() (+16 more)

### Community 20 - "Durable Task Queue"
Cohesion: 0.14
Nodes (11): Any, BaseModel, One unit of deferred work., ScheduledTask, datetime, Take ownership of a task. False when another worker got there first., Return abandoned tasks to the pending pool. Called at the top of a tick,, Durable, deduplicated, leased task storage. (+3 more)

### Community 21 - "Workflow Loading & Validation"
Cohesion: 0.11
Nodes (13): load_definition(), load_directory(), Path, Load every ``*.yaml`` in a directory into a catalog., Definitions, keyed by ``name@vN``., Parse and validate one definition file., WorkflowCatalog, Any (+5 more)

### Community 22 - "Demo Scenario Presentation"
Cohesion: 0.18
Nodes (21): Console, Scenario A — the purchase-order reroute.      "Part X will likely cause produc, run_scenario_a(), act(), approval_card(), audit_summary(), emphasis(), fresh_harness() (+13 more)

### Community 23 - "Mail Commitment Extraction"
Cohesion: 0.13
Nodes (23): _extract_commitments(), _extraction_prompt(), _po_must_be_listed(), BaseModel, Run extraction over each message, skipping ones that cannot be relevant., Reject a commitment attached to a purchase order that was not offered., What a supplier said about when something will arrive., SupplierCommitment (+15 more)

### Community 24 - "Quality Lots & Shortage Flags"
Cohesion: 0.18
Nodes (21): Lot L-2077 - P-1190, wrong part, Lot L-2088 - P-1188, released but committed to 4830, available_lots_for_part(), get_lot(), get_shortage_flag(), _lot(), lots_for_part(), lots_on_hold() (+13 more)

### Community 25 - "Planner & Working Memory"
Cohesion: 0.15
Nodes (13): Any, Everything the current run has learned., The knowledge the planner is shown.          Recalled beliefs are labelled as su, WorkingMemory, PlannerOutput, The schema the planner model is required to fill in.      Flat rather than a dis, Planner, Any (+5 more)

### Community 26 - "SQLite Store & Transactions"
Cohesion: 0.18
Nodes (9): Connection, Cursor, Any, Path, Owns the database connection and the transaction stack.      A single connection, Apply any migrations not yet recorded. Returns the names applied.          ``exe, Reentrant transaction. Outermost commits; nested levels use savepoints., Store (+1 more)

### Community 27 - "Run Explanation"
Cohesion: 0.23
Nodes (9): Any, Reconstructing a run from the audit log alone.  Requirement 7 of the brief: *"fr, Turns a run's audit events into a narrative a person can read., The payload fields worth showing inline.          Curated rather than exhaustive, RunExplainer, _short(), AuditEvent, BaseModel (+1 more)

### Community 28 - "Binding Expression Resolution"
Cohesion: 0.19
Nodes (14): Clock, Protocol, Source of truth for 'now' within a run., BindingUnresolved, A ``${...}`` binding expression could not be resolved., BindingContext, Any, Binding expressions: ``${params.x}``, ``${steps.y.output.z}``, ``${clock.today}` (+6 more)

### Community 29 - "Simulated Clock"
Cohesion: 0.17
Nodes (9): _ClockBase, date, datetime, Current instant, naive and interpreted as site-local time., Last instant of ``day`` (default: today). Used for approval expiry., An advanceable clock whose position is durable.      The position is persisted s, Move the clock forward to ``target``. Returns the new instant., Move forward by a ``timedelta`` keyword spec, e.g. ``advance_by(days=1)``. (+1 more)

### Community 30 - "Workflow Instance Repository"
Cohesion: 0.19
Nodes (8): dump_json(), One in-flight or finished execution of a definition., WorkflowInstance, InstanceRepository, datetime, Loads and saves workflow instances., Instances a restarted process should pick back up., Record a step's result and advance the cursor, atomically.          See the modu

### Community 31 - "Harness Bootstrap & Migrations"
Cohesion: 0.16
Nodes (14): parse_datetime(), Coerce seed/config values into a datetime, defaulting bare dates to midnight., Schema for the harness itself.  Every table here would exist for any customer ru, load_plugin_modules(), Import every module under ``package`` so its decorators run.      Returns the mo, _load_or_start_clock(), _persist_clock(), date (+6 more)

### Community 32 - "Gate Vocabulary & Northfield Rules"
Cohesion: 0.15
Nodes (13): StrEnum, Gate vocabulary: verdicts, rule results, and what a rule gets to look at.  The g, Verdict, Enter a declared workflow with these parameters.      The model chooses *whether, Run these tool calls, in this order.      The free-form path. Every call is stil, ToolPlan, WorkflowInvocation, Config/Code Boundary (+5 more)

### Community 33 - "Mail System & Inbox Filtering"
Cohesion: 0.18
Nodes (16): _might_concern(), A cheap filter before an expensive call.      A message naming a purchase order,, M-002 Manufacturing Weekly newsletter (noise), M-004 Tom Vasquez: line review moved (noise), M-007 Grace to Marcus: confidential headcount (withheld), delete(), get(), inbox_for() (+8 more)

### Community 34 - "Parts, Orders & Supply Seed Data"
Cohesion: 0.16
Nodes (17): Goods Receipt GR-5501 (for PO-77755), P-2218 Drive coupling, 12mm bore, P-3390 Control board, 4-axis, P-4471 Stepper motor, NEMA 23, 2.8A, P-5540 Servo drive assembly, 15kW, Projection Beats Safety-Stock Alarming, Production Order 4808 - PRD-CX180 (in progress), Production Order 4812 - PRD-CX200 Conveyor Drive Unit (+9 more)

### Community 35 - "Approval State & Availability"
Cohesion: 0.17
Nodes (12): AlwaysAvailable, ApprovalState, AvailabilityOracle, date, datetime, Protocol, StrEnum, Approval requests: asking a human, and knowing what to do when they do not answe (+4 more)

### Community 36 - "Context Bundle & Scanning"
Cohesion: 0.23
Nodes (6): Any, Read from the systems this detector declared.          With no subjects, provide, ContextBundle, Any, A compact shape for audit payloads — counts, not contents., Everything gathered for one run, across every reachable system.

### Community 37 - "Error Hierarchy & Scope Denial"
Cohesion: 0.18
Nodes (8): Mint the execution grant a granted approval authorises.          The digest is r, Raise :class:`ScopeDenied` unless every scope is held.          The denial is, HarmonyError, Any, Exception, Base class for all deliberate harness failures., The acting principal lacks a scope required to read or write., ScopeDenied

### Community 38 - "Run Repository & State"
Cohesion: 0.21
Nodes (4): datetime, Move to a new state, persisting and auditing together., Persistence and state transitions for runs., RunRepository

### Community 39 - "Harness Sessions & Startup"
Cohesion: 0.21
Nodes (5): Harness, Fail loudly at start-up rather than quietly at run time.          Everything c, Mint a session acting as a named employee., Mint the narrow non-human session detectors and escalations run under., Everything, wired together.

### Community 40 - "Calendar Availability"
Cohesion: 0.30
Nodes (11): Busy Is Not Absent, Out Tomorrow, Not Out Ever, _covers(), _event(), events_for(), is_out_of_office(), Any, date (+3 more)

### Community 41 - "Runtime Services & Deployment"
Cohesion: 0.20
Nodes (8): Infrastructure a tool or provider needs at call time.  Tools receive ``(sessio, Handles a tool or provider may use while executing., RuntimeServices, Migration, One forward-only schema change, applied once and recorded by name., Deployment, The deployment record: everything the harness needs to know about one company., One company's binding of the harness to its own systems.

### Community 42 - "Scheduled Work Vocabulary"
Cohesion: 0.22
Nodes (8): StrEnum, Scheduled work: the shape of something the harness will do later.  Deferred work, TaskState, The durable task queue.  Small, deliberately. It does the four things a queue mu, The worker: drains due tasks and dispatches them to handlers by kind.  The loop, Register a handler for one task kind.          @task_handler("approval.escalate", task_handler(), TaskHandler

### Community 43 - "Context Broker"
Cohesion: 0.25
Nodes (5): DetectorSpec, A registered detector., ContextBroker, Gathers context across the systems a profile declares., Fetch from each named system the session can reach.

### Community 44 - "Free-Form Plan Executor"
Cohesion: 0.28
Nodes (6): PlanExecutor, Any, The convention this path rests on.          Outputs take precedence over inputs,, Runs a sequence of tool calls, compensating in reverse if one fails., Run every call in order. Raises after compensating if one fails., Undo completed calls in reverse order, best-effort.          Every outcome is au

### Community 45 - "Scenario B Quality Hold Cast"
Cohesion: 0.43
Nodes (8): E-004 Incoming inspection review, M-006 Ingrid: L-2093 placed on hold, P-1188 Bearing race, hardened, 45mm, Production Order 4820 - PRD-BR90 Bearing Housing Assembly, Lot L-2065 - P-1188, scrapped, Lot L-2093 - P-1188, on hold, Priya Raghunathan - Quality Manager, Ingrid Sorensen - Quality Engineer

### Community 46 - "Clock & Worker CLI"
Cohesion: 0.29
Nodes (6): clock_advance(), Drain due scheduled work.      Under a simulated clock there is nothing to wait, Move simulated time forward, firing anything that becomes due., worker(), Runs due tasks. One per process; the lease is what makes more than one safe., Worker

### Community 47 - "Worker Tick & Follow-Ups"
Cohesion: 0.29
Nodes (4): datetime, Run every task due at the current instant. Returns what was run., Tick until nothing is due.          A task may schedule another that is already, Step 8: Schedule Arrival Check

### Community 48 - "Calendar Provider & System Principal"
Cohesion: 0.29
Nodes (5): System Principal (system:harmony), availability_factory(), CalendarAvailability, date, Answers whether someone is available on a day.      Implements :class:`harmony.g

### Community 49 - "Init & Audit Explain CLI"
Cohesion: 0.40
Nodes (5): audit_explain(), init(), Path, Reconstruct a run from the audit log alone., Create the database, apply migrations, and load Northfield's seed data.

### Community 50 - "Tool Schema Description"
Cohesion: 0.50
Nodes (3): Any, The input schema shown to the model., A compact description for the planner's prompt.

### Community 51 - "Binding & Planner Descriptions"
Cohesion: 0.40
Nodes (4): _check_bindings(), Any, Every binding must point at a parameter or an already-available step output., Workflows the profile permits, as the planner is shown them.

### Community 52 - "Lot Hold Detection Noise"
Cohesion: 0.50
Nodes (4): _covering_lots(), Released, unallocated lots of the same part that are large enough alone.      Th, Detector Noise Discrimination, Status Alone Is Not Availability

### Community 53 - "Shortfall Projection"
Cohesion: 0.50
Nodes (4): _project(), date, Work out what will be on hand when the order starts, and show the working., Days Of Cover (derived, never stored)

## Knowledge Gaps
- **12 isolated node(s):** `harmony-agent`, `Approval Policy`, `PO Value Limit Key (po_create_max_value)`, `Escalate To Manager`, `Material Shortfall Horizon (14 days)` (+7 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Session` connect `Tool Definitions & Sessions` to `LLM Client & Replay`, `Gate Rules & Decisions`, `Audit Log, IDs & Durable Memory`, `Principals & User Directory`, `Detector Framework`, `Context Provider Framework`, `Tool Invocation & Execution Grants`, `Workflow Engine Execution`, `Run Lifecycle & Audit Events`, `Orchestrator Loop`, `Approval Service & Escalation`, `Attention Item Dedupe`, `Workflow Definition Models`, `Hash-Chained Audit Log`, `Mail Commitment Extraction`, `Planner & Working Memory`, `Binding Expression Resolution`, `Harness Bootstrap & Migrations`, `Gate Vocabulary & Northfield Rules`, `Approval State & Availability`, `Context Bundle & Scanning`, `Error Hierarchy & Scope Denial`, `Harness Sessions & Startup`, `Runtime Services & Deployment`, `Context Broker`, `Free-Form Plan Executor`, `Calendar Provider & System Principal`?**
  _High betweenness centrality (0.247) - this node is a cross-community bridge._
- **Why does `Store` connect `SQLite Store & Transactions` to `Audit Log, IDs & Durable Memory`, `Principals & User Directory`, `Tool Invocation & Execution Grants`, `Workflow Engine Execution`, `Run Lifecycle & Audit Events`, `Plan Models & Repository`, `Orchestrator Loop`, `Approval Service & Escalation`, `Attention Item Dedupe`, `Workflow Definition Models`, `Hash-Chained Audit Log`, `ERP System Of Record`, `Durable Task Queue`, `Quality Lots & Shortage Flags`, `Workflow Instance Repository`, `Harness Bootstrap & Migrations`, `Mail System & Inbox Filtering`, `Parts, Orders & Supply Seed Data`, `Approval State & Availability`, `Run Repository & State`, `Harness Sessions & Startup`, `Calendar Availability`, `Runtime Services & Deployment`, `Scheduled Work Vocabulary`?**
  _High betweenness centrality (0.204) - this node is a cross-community bridge._
- **Why does `Harness` connect `Harness Sessions & Startup` to `Tool Definitions & Sessions`, `LLM Client & Replay`, `Gate Rules & Decisions`, `Audit Log, IDs & Durable Memory`, `CLI Command Surface`, `Tool Invocation & Execution Grants`, `Workflow Engine Execution`, `Run Lifecycle & Audit Events`, `Plan Models & Repository`, `Orchestrator Loop`, `Approval Service & Escalation`, `Attention Item Dedupe`, `Hash-Chained Audit Log`, `Tool Catalog & Compensation Checks`, `Durable Task Queue`, `Demo Scenario Presentation`, `Planner & Working Memory`, `SQLite Store & Transactions`, `Simulated Clock`, `Harness Bootstrap & Migrations`, `Run Repository & State`, `Runtime Services & Deployment`, `Scheduled Work Vocabulary`, `Context Broker`, `Clock & Worker CLI`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Are the 73 inferred relationships involving `Session` (e.g. with `DetectorContext` and `DetectorSpec`) actually correct?**
  _`Session` has 73 INFERRED edges - model-reasoned connections that need verification._
- **Are the 27 inferred relationships involving `Store` (e.g. with `AuditLog` and `AuditWriter`) actually correct?**
  _`Store` has 27 INFERRED edges - model-reasoned connections that need verification._
- **Are the 29 inferred relationships involving `EventType` (e.g. with `RunExplainer` and `AuditLog`) actually correct?**
  _`EventType` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Are the 29 inferred relationships involving `ToolFailed` (e.g. with `ToolInvoker` and `ApprovedSuppliersInput`) actually correct?**
  _`ToolFailed` has 29 INFERRED edges - model-reasoned connections that need verification._