# DESIGN.md

Written answers for the parts I didn't build – and one for a part I did.

---

## Would I build the workflow engine this way again?

> *If you were designing a deterministic workflow engine from the start, where reasoning is just a small node inside the graph rather than the thing that drives it, would you still build it the way you did here?*

**No. I'd invert it.**

### The tell

Look closely: I've got two engines running side‑by‑side. `harmony/runtime/orchestrator.py` drives a state machine over `runs`, and `harmony/workflow/engine.py` pushes a cursor through `workflow_instances`. Both persist after every transition. Both resume. Both audit. Both compensate. That duplication isn't accidental – it's what happens when you solve the same problem twice at two different levels, and once you spot it, you can't unsee it.

The smoking gun is `AWAITING_APPROVAL`. That's just a run suspended on an external signal – a perfectly ordinary thing for any durable execution engine to represent. Yet here it's special‑cased: a dedicated run state, a separate `ApprovalRequest` record, a scheduled escalation task, and bespoke resumption logic in `Orchestrator._decide`. A proper workflow engine that already knows how to pause on a signal would have given me that for free.

### The inversion

If I were starting fresh, the durable execution graph would be the top‑level primitive, and the *entire agent run* would be a workflow definition – detect, gather, plan, gate, approve, execute, follow up. `plan` becomes an LLM node. `approve` becomes a human‑task node that suspends the instance until an external signal arrives.

What that buys:

- **One persistence model instead of two.** `runs` and `workflow_instances` collapse into instances of different definitions.
- **One resume path.** Today, resuming a workflow and resuming an approval are different code with different bugs – that goes away.
- **One compensation model**, which matters most for the next point.
- **The free‑form path inherits the guarantees.** This is the concrete win. Today a model‑assembled plan gets weaker treatment than a declared one, and `harmony/execute/executor.py` documents exactly how:

  | | declared workflow | free‑form tool plan |
  |---|---|---|
  | step order | fixed by definition | chosen by the model, then frozen |
  | resumable | yes, from a cursor | no – a killed run is re‑planned |
  | compensation input | declared bindings | *inferred* from the original call |
  | preconditions | `on_empty: fail` | none |

  That third row is the sharp one. A workflow states exactly how to undo a step. A free‑form plan falls back to a convention: invoke the declared compensation with the original parameters merged with the outputs, and record a failure if it doesn't validate. It usually works, because compensations tend to take the identifier their forward tool produced. "Usually works" is not a rollback guarantee.

  Under the inversion, a `dynamic_plan` node emits a small graph, the gate checks it, and the same engine runs it. Scenario B would get resumption and declared compensation instead of approximating both.

### What it costs

You lose the joy of writing orchestration in plain Python – and right now the orchestrator is readable top‑to‑bottom by someone who's never seen this codebase, which is worth something. Every emergent behaviour has to be expressible as a graph, so the first genuinely novel thing you want the agent to do becomes an engine change rather than fifty lines. Debugging moves into the interpreter, where stack traces are worse and the thing that failed is data.

Temporal and AWS Step Functions made exactly this trade. They're also both harder to start with than a for‑loop, and that's not accidental either.

### Why I split it here, and what that reasoning is worth

The outer loop's shape is stable and engineering‑owned; nobody outside engineering will ever read the orchestrator. The inner one is business‑owned and versioned – a purchasing analyst can read `po_reroute.v3.yaml` and tell you whether it matches what they asked for. Different audiences, different rates of change, different representations.

That's a real distinction. It's also **expedient rather than principled**, and I wouldn't defend it as the right architecture. It's the right architecture *given that I was writing the outer loop from scratch in three days and a durable execution engine is a quarter's work*. If the platform is going to run thousands of these, the inversion is where it ends up, and the migration cost only grows.

### One thing I would add either way

The step order in `po_reroute.v3.yaml` is purchasing's order verbatim, and it also happens to be sorted by reversibility: every compensable write precedes the one irreversible effect. That's not luck, but it's also not something the business articulated – they put "notify production" last because it's last in the story, and it happens to be the only step that can't be undone.

An engine could *enforce* that: refuse to load a definition in which an irreversible step precedes a compensable one. This one doesn't; it only requires each write to declare which it is (`harmony/workflow/loader.py`). Enforcing the ordering would catch a whole class of workflow that looks fine and rolls back badly, and it's about twenty lines.

---

## Identity and authorization

### How a real deployment establishes who the user is

OIDC or SAML SSO establishes the human, once, at the edge. Per run, the harness performs **OAuth 2.0 token exchange (RFC 8693)** against a token broker, trading the user's identity token for short‑lived, audience‑bound, downscoped per‑system access tokens – a SAP OData scope for the ERP, a delegated `Mail.Read` for Microsoft Graph, and so on.

The `Session` object (`harmony/identity/session.py`) already has the right shape. What changes is where its scopes come from: today they're read from seed data, in a real deployment they're the claims of exchanged tokens. The session holds token *references*, materialised just‑in‑time at the tool boundary, so no long‑lived secret sits in a run's memory and the agent holds no standing credentials.

### How permissions flow to the tool layer

Three properties, and the second is the one that distinguishes this from a permissions check bolted onto an agent.

**The effective scope set is an intersection**, and it's implemented rather than aspirational:

```
user's entitlements  ∩  the profile's declared needs  ∩  this run's stated purpose
```

Dana holds `mail:send`. Her profile never asks for it. Her agent cannot send mail as her. There's no code path that *widens* a live session – `Session.downscope` can only narrow – which is what makes prompt injection a bounded problem rather than an unbounded one. The worst a compromised prompt achieves is the profile's blast radius, not the person's.

**On‑behalf‑of tokens give defence in depth.** With real delegated tokens the ERP performs its own authorization, so my gate is the second line rather than the only one. It also fixes a forensics problem nobody thinks about until an auditor asks: the ERP's own audit log records *Dana*, not "the automation service account". A system where every agent action appears in the systems of record as the same non‑human principal is one where you can't answer "who bought this?".

**Approval is a separate artifact from authorization.** Scopes say what a person may do in general; an `ExecutionGrant` says what a human agreed to *this time*. It's bound to a proposal digest and carries an explicit allow‑list of tool names, so an approved reroute authorises the reroute's tools and nothing else – even where the principal's scopes would permit more. If the plan changes between approval and execution, the digest stops matching and the write refuses.

### The offline problem

This is the corner a security review will spend its time on, so it's worth naming unprompted.

Detectors run on a schedule, while the employee is asleep or out of office. Somebody has to act, and it can't be them. This build uses a system principal holding `calendar:freebusy:read` and `harmony:schedule:create` and **no write scope of any kind** (`northfield/policy.yaml`), so every change to a system of record traces to a named employee who approved it.

That works here because detection only reads. It gets harder in a real deployment, because a system principal must be able to *read enough to detect* on behalf of someone who isn't present – which is a delegation grant, needs an expiry, needs to be revocable, and needs to be visible to the person it acts for. The design I'd argue for: detection runs under a delegation the user grants explicitly and can inspect, scoped to exactly the detectors on their profile, renewed on login, and audited on every use. What must never happen is a service account that can read everything because it was easier than modelling the delegation.

The first place this bites is already visible: the end‑of‑day escalation reads whether an approver is out tomorrow. That runs as the system principal with free/busy access only, and returns a boolean rather than a diary entry – reading a colleague's calendar isn't something the *requester* should need rights for just because the harness wants to route an approval.

---

## Long‑term memory

Two rules carry the whole answer.

### Rule 1: memory holds derived judgments, never system‑of‑record state

"Supplier Y has slipped three of its last five commitments" is a judgment. It took work to derive, it stays roughly true, and it's useful to a run that hasn't looked at shipping history.

"PO-77812 is open" is *state*, and memory must never hold it. The ERP is where that question is answered, and a cached copy is a lie waiting to happen.

### Rule 2: memory is strictly advisory

A recalled fact may influence the planner's ranking or phrasing. It may **never** satisfy a gate condition or supply a tool parameter.

So a stale belief can make the agent's *suggestion* worse – which a human then rejects, because the human is still in the loop – but it can never make its *actions* unsafe. **That asymmetry is the entire staleness defence**, and it's cheaper and far more reliable than trying to keep every belief fresh. It's also why recalled facts reach the prompt under a separate heading with `advisory_only: true` attached, rather than mixed in with context the agent actually read.

### What gets promoted, and what carries with it

Promoted: judgments that are corroborated across several runs, decision‑relevant, and structurally stable. Supplier reliability trends. Preferences observed repeatedly ("Dana reschedules production before she expedites"). Structural facts like "P-4471 is effectively single‑sourced".

Not promoted: raw context, system‑of‑record state, one‑off observations, anything a single run asserted once.

Each fact carries provenance (which runs, which source system, observed when), a confidence, a TTL, and a refresh predicate – how to re‑derive it.

### Four defences against staleness

1. **State is never memory.** Current facts are re‑read at run time, always.
2. **TTL, with re‑derivation on read.** A belief nobody consults doesn't need tidying; one that is consulted must be current.
3. **Contradiction demotes.** A run that observes data disagreeing with a stored fact demotes it and audits the demotion – the belief is kept, not deleted, because "we used to think this and stopped" is itself auditable.
4. **Advisory‑only**, as above.

### Honest about the implementation

`harmony/memory/durable.py` implements the interface and very little behaviour. It promotes, recalls, expires and demotes; nothing in the shipped scenarios exercises it hard. The interface is the part I was designing, and I'd rather ship a correct one thinly implemented than a rich store with the wrong rules baked in.

The first thing I'd promote if I kept going is supplier on‑time performance derived from goods‑receipt history. It's the fact `choose_supplier` most wants, it's expensive to compute per run, and it's exactly the sort of judgment nobody looks up by hand.

---

## Scaling to thousands of employees

### Where it breaks first, in order

**1. The single SQLite writer and the single‑process tick loop.** This is the wall, and it arrives at roughly one plant, not one company. The HTTP surface already made it concrete: standing up a server exposed that `Store` held one connection bound to its creating thread, and access is now explicitly serialised under a lock. That's the single‑writer design stated honestly rather than a workaround – two threads can't write concurrently, by construction.

**2. Detection fan‑out.** Polling every detector for every user on a schedule is O(users × detectors × providers) per tick, and most of it is wasted: nothing changed for most people most of the time.

### What changes

- **Postgres**, partitioned per tenant. The audit log becomes an append‑only event store with a separate read model – and the hash chain, which is lovely at one plant, becomes a write‑throughput problem at ten thousand employees. You'd chain per run and per tenant rather than per event.
- **Detectors go event‑driven**, off an ERP change stream (CDC or webhooks), with polling demoted to a backstop for missed events. Detectors already take their input from a context bundle rather than reaching for the database, so the change is in how they're *triggered*, not in what they do.
- **The scheduler becomes a real queue** with leases and visibility timeouts. The handlers are already idempotent, which is the part that normally gets retrofitted painfully after the first duplicate‑execution incident.
- **The workflow engine becomes Temporal.** Keep the definitions; swap the interpreter. And see the Part 2 answer – at that point the inversion is no longer optional.

### What does not change

Provider and tool interfaces. The gate and its rules. The audit event schema. The workflow definitions. The profiles.

That's the payoff for the kernel/company split, and it's the difference between "I would rewrite it" and "I would re‑host it".

### The non‑obvious part

**Detection is code‑only and therefore cheap. Only *runs* cost tokens.**

So the fan‑out control point is **attention‑item creation, not detector execution**. Per‑user rate limits and priority budgets belong at `AttentionItemStore.admit`, where the decision "is this news?" is already being made – not at the worker, where the usual instinct puts them.

Dedupe is already doing part of this job, and it's the reason the cost curve isn't simply linear in users: a detector that fires every tick produces one run, not one per tick. What's missing at scale is the budget – a cap on how many runs one person's agent may open in a day, and a priority order for which get opened when the cap binds. Without it, one badly‑configured detector consumes the token budget for a plant.

---

## Connecting real systems

The provider and tool interfaces survive. What changes is everything about the transport: pagination, delta queries, rate limits, partial failure, and eventual consistency – a purchase order created through the API may not be readable for several seconds, which the free‑form executor's compensation convention would quietly get wrong.

Two things worth saying that are easy to miss:

**Idempotency keys must map onto each target's native mechanism.** Graph's `client‑request‑id`, SAP's `If‑None‑Match`. Today the key is internal, which is fine because the fake ERP has no opinion. Against a real system, having two independent notions of "already done" is worse than having none – you get a local replay that returns a cached result while the remote system performed the write twice.

**Compensation gets genuinely harder.** Real ERPs don't always let you un‑cancel, and suppliers act on cancellations. `erp.restore_purchase_order` already admits this in its docstring. The consequence is that ordering steps by reversibility matters *more* against real systems, not less – and it strengthens the case for an engine that enforces the ordering rather than merely recording it.

For a document store, the retrieval provider needs **ACL‑trimmed search**, applied before ranking rather than after. Trimming afterwards means the ranker has already seen documents the user can't read, and that's where most enterprise RAG deployments leak.

---

## Observability and evaluation

### What I would trace

One span tree per run, with spans per lifecycle stage. LLM spans carrying prompt and response hashes, token counts, latency and call site. Tool spans carrying the idempotency key and whether the call was a replay.

The audit log is already most of a trace – it has the events, the actors, the causal order and the payloads. What it lacks is duration and a span hierarchy. I'd emit OpenTelemetry alongside rather than replacing it, because the audit log answers "was this permitted and who agreed to it?" and a trace answers "why did it take ninety seconds?", and conflating those produces something bad at both.

### What "the agent did a good job" means measurably

- **Attention‑item precision.** Of the items raised, how many were real problems? A detector that alarms on everything is easy to write.
- **Proposal acceptance rate**, split by whether the human edited before approving.
- **Approval‑to‑execution success rate.** Approved plans that then failed are worse than plans that were never approved, because a human spent attention on them.
- **Compensation rate**, and how often compensation itself failed.
- **Detection lead time against time‑to‑impact.** Noticing a shortfall the day before the line stops is technically a detection and practically useless.
- **Human edit distance on drafted text.** The cheapest quality signal in the system, and almost nobody instruments it.

### Catching a regression before users do

This one is built rather than proposed. `northfield/eval/cases.yaml` holds six golden cases; `harmony eval` replays them and `harmony eval --live` runs the same cases against the real model, which is the check to run before shipping a prompt change.

Two design decisions in it are worth explaining:

**Cases assert on properties of the decision, never on wording** – action kind, workflow entered, parameters supplied, evidence cited, traps avoided. Asserting on text gives a suite that fails on every rewrite, which is a suite people delete.

**Half the cases assert silence.** A suite of only positive cases passes an agent that alerts on everything, and that's the easiest failure mode to ship.

Writing them surfaced a third: a `forbids` check against the whole proposal punished the model for *naming a trap in order to reject it*, which is exactly the behaviour you want. It now checks the proposed action only. Punishing stated reasoning trains a model towards silent avoidance, which is harder to audit and no safer.

What's still missing: nothing here measures wording quality. That needs human edit distance on drafted notifications and a judge model calibrated against human labels, plus shadow‑mode for new prompt versions – run the candidate alongside production, compare proposals, and promote only when a human has reviewed the diffs.

---

## In‑flight workflow instances across a version change

Not implemented; the brief said it was a design question.

Instances pin the definition version they started with, so a running instance is never confused by a new one. The interesting cases are what you do *deliberately*:

**Let it finish on the old version.** Correct default, and correct for almost every change. A reroute that started under v3 finishes under v3.

**Drain and republish.** For a change that is urgent – a step that was doing something unsafe – you stop new instances entering v3, let existing ones finish, and publish v4. Needs a "deprecated, no new instances" state on a definition.

**Migrate mid‑flight.** Only safe when the new version's steps up to the current cursor are identical to the old one's, which a loader can check by comparing step ids and inputs prefix‑wise. Anything else is asking an operator to reason about whether step 5 of v4 makes sense given step 4 of v3 was what actually ran, and they will get it wrong.

**Abandon and compensate.** For a version change that invalidates work already done. Expensive and loud, which is right.

I'd ship the first two and refuse the third unless the prefix check passes.
