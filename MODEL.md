# MODEL.md — what I modelled, and why

Northfield Manufacturing builds conveyor drive units and bearing housing assemblies
across three lines. Seven people are modelled, five of whom matter. The company is
deliberately small: the brief asked for as little as the scenarios allow, and every
entity and field below exists because a scenario or a control needs it. Where
something is missing, that is a decision, and it is listed at the end.

The starting position is Monday 2026-09-02, 08:00, on a simulated clock.

---

## Harness tables versus company data

Worth drawing first, because it is the distinction reviewers most often collapse.

**The harness ships these, and they would exist for any customer**
(`harmony/kernel/migrations.py`):

`runs` · `audit_events` · `attention_items` · `proposals` · `approval_requests` ·
`workflow_instances` · `scheduled_tasks` · `idempotency_records` · `memory_facts` ·
`clock_state`

**Northfield ships these** (`northfield/migrations.py`):

`users` · `parts` · `suppliers` · `purchase_orders` · `production_orders` ·
`goods_receipts` · `quality_lots` · `shortage_flags` · `messages` ·
`calendar_events` · `notifications`

Two migration sets, applied to one database, registered separately and never mixed.
Swapping the second package swaps the customer.

---

## Entities: kept, changed, added, omitted

### Kept from the sample, essentially as given

| Entity | Note |
|---|---|
| `parts` | `on_hand`, `daily_usage`, `safety_stock`, `unit_cost`, `lot_tracked`. Days of cover is derived, never stored — a stored derived value is a value that can be wrong. |
| `suppliers` | Including the `approved` / `approved_parts` split, which does more work than it looks like. See the noise inventory. |
| `purchase_orders` | Plus `replaces_po`, so a reroute leaves a traceable chain. |
| `production_orders` | Components as a list, no BOM explosion. |
| `quality_lots` | `status`, `allocated_to`, and the hold fields. |
| `messages`, `calendar_events` | Shapes as given. |
| `users` | Plus the scope vocabulary, below. |

### Added

**`goods_receipts`.** The smallest addition with the largest effect on honesty.
Without it the Tuesday arrival check has nothing to check: it would be asking "does
the purchase order still say it will arrive?" rather than "did the goods arrive?".
Those are different questions and only the second is evidence. The seed data
contains a receipt for the historic order `PO-77755` and none for the one under
test, so the follow-up makes a real observation rather than a scripted one.

**`on_time_rate` on suppliers.** Gives the bounded model step a basis other than
price. Without it `choose_supplier` is a `min()` wearing a prompt; with it, Meridian
at £46.50 with a 94% record is a defensible choice over Kestrel at £42.00 with 62%,
and the model has to say why. It also seeds the long-term-memory discussion in
DESIGN.md — supplier reliability is exactly the kind of derived judgment that should
persist between runs.

**`shortage_flags`.** Scenario B's escalation path, when no lot can cover a
requirement. Deliberately a *record* rather than an email, so that purchasing's own
detectors could pick it up. One person's agent creating work another person's agent
can find is the shape the platform is ultimately for, and an email would have made
that a human hand-off instead.

**`notifications`.** So "was the supervisor told?" is answerable from the system
rather than only from the audit log. It also makes the irreversibility of
`production.notify_supervisor` concrete: the row could be deleted, and the person
has still read it.

### Changed

**Inbound and outbound mail share one table**, distinguished by `direction`. A
notification the agent sends is the same kind of object as an email it read, and
separate tables would mean the audit could not show a thread.

**Lot tracking folded into `quality_lots`** rather than a separate inventory-lot
entity. The brief said keep it small; this is where I obeyed it.

### Omitted

Each of these is realistic and none of them changes an interface — which is the
point of the second clause.

- **BOM explosion and multi-level MRP netting.** `material_shortfall` uses a 14-day
  horizon with `daily_usage` as baseline consumption, standing in for a planning
  run. A real MRP would net demand across all orders and levels; it would produce a
  better projection through the same detector interface.
- **Currencies.** Everything is sterling.
- **Receiving inspection as a workflow.** Lots arrive already released or on hold.
- **Cost accounting, landed cost, freight.** The value threshold uses unit price ×
  quantity, which is what a buyer's authority limit is actually written against.
- **Partial shipments and multi-lot splits.** The covering-lot search asks whether a
  single lot covers the requirement. Splitting across two is realistic and the seed
  data has a distractor for it (see L-2088), but modelling it would add an
  allocation solver without exercising anything new in the harness.

---

## A simplification worth stating plainly

`material_shortfall` **skips lot-tracked parts entirely.** For those, "how much is
available?" is not answered by an aggregate on-hand figure — a quality hold can make
a well-stocked part unavailable, and a released lot can make a thin one fine.
`lot_hold_allocation_risk` answers it properly, against the lots themselves.

I found this by running the detector: it raised a shortfall on P-1188 that was an
artefact of projecting against a total that lot status made meaningless. Excluding
lot-tracked parts is the correct domain answer, not a workaround.

---

## The noise inventory

The brief asked for noise. Each distractor below exists to make a specific control
falsifiable — if the control broke, one of these would produce a wrong answer.

| Distractor | What it catches |
|---|---|
| **Apex Rapid Supply (S-Q)** — approved *vendor*, £38.00 against Meridian's £46.50, next-day delivery, and an unsolicited quote sitting in Dana's inbox (M-005). Not on the qualification list for P-4471. | The whole point of separating `approved` from `approved_parts`. Refused at three independent layers: the workflow's step 1 never surfaces them, the `approved_supplier` gate rule denies a hand-built plan naming them, and `erp.create_purchase_order` refuses at the point of effect. Failure case 4 exercises all three. |
| **Halstead Precision (S-W)** — qualified for the part *and* cheaper than Meridian. Nine-day lead time. | Makes step 2 a real gate rather than a lookup. Removed by arithmetic in a tool, before the model sees the list, so the cheapest qualified option is excluded for a reason that is auditable. |
| **Production order 4835** — consumes P-4471, starts 2026-09-25. | Detector precision. An agent keyed on "this part is short" rather than "this order will not be covered" raises it. It must stay silent. |
| **P-2218 within order 4812** — the same at-risk order, comfortably stocked. | Being a component of an at-risk order is not itself a risk. |
| **P-3390** — below safety stock (48 against 60), with PO-77801 arriving 2026-09-03. | A naive safety-stock detector raises it. Projected supply against demand does not. |
| **M-003** — same supplier, same week, reads exactly like a delay notice, and reports that PO-77790 is *on schedule*. | Extraction precision. Confirmation is not revision, and a model that conflates them raises a false alarm against a healthy order. |
| **M-007** — between two other people entirely. | Provider scoping. Dana's agent must not see it, and the audit must record that it was **withheld** rather than absent. |
| **L-2065** scrapped, **L-2088** released but committed to order 4830, **L-2077** available but the wrong part. | One distractor per filter in the covering-lot search. |
| **E-002** — a meeting on the very day in question. **E-005** — out of office three weeks later. | Busy is not absent, and "out tomorrow" is not "ever out". A rule that confused either would route approvals away from people sitting at their desks. |

### The distractor that carries the argument

On its promised date of 2026-09-04, PO-77812 arrives three days before production
order 4812 starts. **There is no problem at all.**

What creates the shortfall is one sentence of prose in M-001: *"Revised ship date is
Monday 9/7, which puts it on your dock Tuesday 9/8."* Nothing structured says this.
No amount of parsing gets `2026-09-08` out of it reliably.

That is the load-bearing case for having a model in the loop, and it is asserted:
`test_the_delay_email_is_what_creates_the_problem` deletes M-001 and checks the
alarm goes away. Everything downstream of the extracted date is arithmetic.

---

## People and permissions

Scopes follow `system:object:verb` and are opaque strings to the harness — matched,
intersected and compared, never interpreted.

| | Role | Notable scopes | Notable absences | Limit |
|---|---|---|---|---|
| **u-100** Grace Okonkwo | Director of Operations | read-only across ERP | — | £250,000 |
| **u-101** Dana Whitfield | Purchasing Manager | `erp:po:create`, `erp:po:cancel`, `production:notify`, `mail:send` | — | £25,000 |
| **u-102** Marcus Bell | Senior Buyer | same as Dana | — | £15,000 |
| **u-202** Priya Raghunathan | Quality Manager | `quality:lot:allocate`, `purchasing:shortage:flag` | **no `erp:po:create`** | lot qty 500 |
| **u-301** Tom Vasquez | Production Supervisor | `erp:production:read` | no writes | — |
| **u-303** Alex Mercer | Production Planner | `erp:po:read` | **no `erp:po:create`** | — |

Two absences do the work.

**Priya cannot raise a purchase order.** That is why Scenario B's escalation path is
a shortage flag rather than an order — escalation *across* a permission boundary
rather than around it.

**Alex cannot either**, and he is deliberately cc'd on M-001. His agent runs the
same detector over the same data and reaches the same conclusion Dana's does. The
gate denies his plan whole, before anything runs. Seeing a problem and being allowed
to fix it are different things, and failure case 1 turns on exactly that.

A third layer sits above the user: **a profile can only narrow.** Dana holds
`mail:send`; `purchasing_manager.yaml` neither asks for the scope nor lists a
mail-sending tool; her agent cannot write to her mailbox even though she can. The
effective scope set is `user's entitlements ∩ profile's declared needs ∩ this run's
purpose`, and no code path widens a live session.

---

## The clock

One simulated clock, starting 2026-09-02T08:00, persisted in `clock_state`, and
forward-only. Forward-only matters: a backwards clock would make already-fired
scheduled tasks come due again.

Nothing in either package reads the wall clock — `datetime.now()` appears only in
`harmony/kernel/clock.py` and in the audit log, which records both simulated and
real time so a reader can distinguish "the agent waited five days" from "the demo
ran in a fifth of a second". `tests/architecture/` enforces this. Without it the
follow-up would be the feature that silently broke.

---

## What a real model would add, and why it would not change the architecture

Plant hierarchies and multi-site inventory. Real BOMs with phantom assemblies.
Blanket orders and call-offs. Supplier scorecards with more than one dimension.
Inspection plans and certificates of conformance. Serial-number traceability.

Every one of those is more rows and more fields behind the same four provider
interfaces and the same tool contract. The thing that would genuinely change the
architecture is not more data — it is a second *kind* of question the agent has to
answer, and the test of that claim is Scenario B: adding a whole new system of
record cost five files, all under `northfield/`, and no change to the orchestrator,
the planner, the gate or the audit layer.
