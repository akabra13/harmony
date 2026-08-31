# Enterprise Agent Harness — My Approach

## The Challenge

We're building something pretty ambitious: a personal AI agent for every employee at a manufacturing company. These agents need to understand each person's role, monitor all their data streams (ERP, email, calendar, internal docs), spot problems before they escalate, and take action—all while respecting permissions, requiring human approval for important decisions, and clearly explaining every step.

I've designed a modular system that handles all of this. Let me walk you through the core scenario and how it all fits together.

---

## Scenario A: The Purchasing Manager's Agent

Here's the situation: Dana is a purchasing manager. One of their critical parts is going to run out in five days. There's an open purchase order with Supplier Y, but that supplier just emailed saying the shipment is delayed. Meanwhile, production order 4812 needs that part and is scheduled to start before the new delivery date.

The agent should:

1. **Notice this on its own** (through scheduled checks or when new data arrives)
2. **Pull together information** from the ERP system, Dana's email inbox, and their calendar
3. **Figure out** that rerouting the order to Supplier Z is the right move
4. **Check** if Dana has permission to do this and if the PO value needs manager approval
5. **Ask Dana** for approval with a clear explanation
6. **Execute** the plan once approved—create the new PO, cancel the old one, notify production, and schedule a follow-up check
7. **Keep a complete log** so anyone can later see what the agent saw, decided, and did

The approval part gets interesting: if Dana doesn't respond by end of day and their calendar shows they're out of office tomorrow, it automatically goes to their backup.

---

## What I've Built

### Core Systems I Modeled

I kept things focused on what we actually need for this scenario. Instead of building a giant fake ERP, I modeled:

- **Parts** with inventory levels and usage patterns
- **Suppliers** with approval status, lead times, and pricing
- **Purchase orders** with status and delivery dates
- **Production orders** with components and schedules
- **Quality lots** for the second scenario (more on that later)
- **Email** to detect supplier communications
- **Calendar** to check availability and out‑of‑office status
- **Users and permissions** with role‑based access controls

### The Harness Architecture

I split responsibilities into these replaceable pieces:

1. **Detection Engine** — runs on a schedule or when data changes, looking for attention‑worthy situations
2. **Context Providers** — each system (ERP, email, calendar) has its own way of fetching data, respecting what the user can actually see
3. **Planner** — takes the attention item and all the context, then decides what to do
4. **Gate** — enforces permissions, checks policy thresholds, handles approval routing
5. **Executor** — runs approved actions with idempotency and compensation (undo if something fails)
6. **Memory** — tracks what this run knows versus what persists between runs
7. **Scheduler** — handles deferred work that can survive a restart
8. **Audit Log** — append‑only record of every step, decision, and outcome

---

## The Workflow Piece (Part 2)

Purchasing told us something important: they don't want the AI improvising when it comes to PO reroutes. The steps are fixed:

> Confirm alternate supplier is approved → Confirm lead time meets production date → Create new PO → Cancel old PO → Notify production → Schedule arrival check

So I built a **declarative workflow engine** that handles this. The AI still decides *that* a reroute is needed and provides the parameters, but once we enter the workflow, the step order is fixed. The model can't reorder, skip, or add steps.

Each step is idempotent and has compensation logic built in. State persists after each step, so if the system crashes, it can pick up where it left off.

**Design thought:** If I were designing this from scratch, I'd probably lean even more into the workflow‑as‑first‑class concept, with reasoning being just one node type in the graph. The way I built it works, but there's definitely a cleaner architectural separation I'd explore.

---

## Scenario B: Quality Management

After the first scenario was working, I added another one: a quality manager's agent notices a lot of parts got placed on quality hold, and a production order needs that lot in three days. The agent should:

1. Notice the hold
2. Find if another good lot can cover it
3. Either reallocate the good lot and notify production, or flag a shortage to purchasing if no lot can cover it

This required adding quality/lot data, a new detector, extending the context provider, and adding a new tool. I didn't have to modify the planner, gate, or audit layer significantly—the existing abstractions handled it fine.

---

## Design Decisions I'd Make for Production

### Identity and Authorization
In a real deployment, we'd use SSO and token exchange. The agent wouldn't hold standing credentials—instead, we'd pass a user context through the whole chain, with each system checking permissions against that context before returning data or executing actions.

### Long‑term Memory
I'd promote certain things from runs into durable context: supplier performance history, common reroute patterns, user preferences. Keeping it accurate means having clear invalidation rules and a way to surface updates when source systems change.

### Scaling to Thousands
My current design would break at the planner first—a single model instance making decisions for everyone isn't going to cut it. I'd need:
- Parallel planning instances with proper isolation
- Cached context to reduce duplicate fetching
- Rate limiting and queueing for tool execution
- Better monitoring to catch when any part of the system is struggling

### Real System Integration
The provider and tool abstractions I built would map fairly cleanly to real systems:
- ERP API → our ERP provider with read/write capabilities
- Microsoft Graph → email and calendar providers
- Document store → a new provider type with its own permissions

The core harness wouldn't change much—just the implementations of the connectors.

### Observability
I'd trace everything: planner latency, tool execution times, approval response times, workflow step success/failure rates. "Good" would be measured by:
- Approval rates (high = good recommendations)
- Reroute success rates
- False positive rate on detections
- Time from detection to resolution

Catching regressions would mean comparing these metrics over time and having human spot checks when things look off.

---

## What I'm Most Proud Of

The integration between the free‑form planner and the deterministic workflow is probably the trickiest part I handled. The agent decides to reroute, fills in the parameters, and then the workflow takes over with its fixed step order. Both paths stay exercised (Scenario A uses the workflow, Scenario B is more free‑form), so the system stays flexible where it should be and disciplined where it needs to be.

The audit trail is another point of pride—every step gets logged with what the agent saw, what it decided, who approved what, and what actually happened in each system. From that alone, you can reconstruct the entire decision chain.

---

## What I Cut and Why

I didn't build:
- **A full UI** — CLI is fine for this demo, and UI design would be a whole separate exercise
- **All 40+ ERP tables** — I modeled only what the scenarios needed
- **Real‑time event streaming** — scheduled polling works for the demo, though I'd use webhooks in production
- **Multi‑language support** — not relevant for this phase
- **Advanced workflow versioning** — I designed for it but didn't implement live migrations
- **Performance optimization at scale** — not the focus for a demo

These were conscious tradeoffs to keep the core architecture clear and functional within the timebox.

---

## Running the Demo

One command runs everything: Scenario A through approval, execution, clock advancement to Tuesday for the follow‑up, Scenario B, and failure cases. The audit log shows the full decision trail.

---

That's the approach. It's focused on getting the core scenarios right while being designed for the realities of an enterprise deployment.