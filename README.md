# Discern Platform

Integrated operations platform for Discern Engineering Pvt Ltd — a purpose-built system covering the full chain from enquiry to project cost across multiple simultaneous construction/EPC projects.

```
Enquiry → Sales → Project → BOQ → Procurement → Fabrication / Subcontract → Receipt → Costing
```

**Status: working application covering Phases 0–3, with role-based administration. 192 tests passing. Sign in with `demo` / `discern2026`.**

---

## The problem

Discern runs several construction projects at once, each with its own client, site, budget and material requirement. Today information is re-typed at every handoff — a lead becomes a quotation, a won job becomes a project, someone works out the materials, someone else raises purchase orders, someone at site receives them — and the question *"what is this project costing us right now, against what the client agreed to pay"* takes a month-end reconciliation to answer.

This platform makes that one question a live query, and removes the re-typing between every stage.

---

## Design documents

Read in order. Each builds on the previous.

| | Document | Covers |
|---|---|---|
| 1 | [**Process Design**](docs/01-process-design.md) | The core document. Design principles, the eleven concepts the system rests on, the process stage by stage, cross-cutting mechanisms, roles |
| 2 | [**Architecture**](docs/02-architecture.md) | Modular monolith, stack and why, the three ledgers, approval engine, event bus, data integrity, operations |
| 3 | [**Data Model**](docs/03-data-model.md) | Entities by module, key fields, and an entity-relationship summary |
| 4 | [**Build Plan**](docs/04-build-plan.md) | Six phases with honest effort ranges, sequencing constraints, migration and cutover, risks |
| 5 | [**Decisions Register**](docs/05-decisions.md) | 8 blocking decisions; ~30 policy defaults to accept or override; 6 questions the design already settles |
| 6 | [**Flowcharts**](docs/06-flowcharts.md) | Eight focused diagrams, rendered inline by GitHub |
| — | [**Progress Log**](docs/PROGRESS.md) | Current state, what's next, decisions taken — read this first when picking the work back up |

---

## The five ideas that carry the design

**1. Ledgers, not counters.** Cost, committed quantity and stock on hand are *derived* by summing append-only entries. Nothing increments a stored total, so nothing can silently drift. A sum is either correct or provably not.

**2. The Lot is a first-class entity.** Between the order and the BOQ sits the *lot* — one commercially-priced deliverable, itemized or lump-sum SITC. Every BOQ line, commitment and cost entry carries its lot, so margin per SITC lot is an ordinary query rather than a reporting project.

**3. One BOQ, two discipline-owned sections.** The Design Manager owns the Goods section, the Construction Manager owns the Service section, both in one revision with one release. Ownership is preserved; two revision streams that can drift apart are not. An empty section is *not applicable* and releases normally — so a materials-only project cannot deadlock.

**4. The quantity ceiling is a commitment ledger.** Every document authorizing quantity against a BOQ line writes one signed entry through one function under a row lock. Returns, cancellations and amendments net out arithmetically, because releasing headroom is consumption with the opposite sign. A fifth document type added later gets the ceiling for free.

**5. Everything converges on the cost ledger.** Material, fabrication, subcontract, site expenses, stock transfers and revenue all post to one append-only table tagged with project and lot. There is no category of real spend that can fail to reach the profitability figure.

---

## Relationship to the earlier Odoo design

This supersedes *Discern CRM to Purchase Process Design* rev. 17, which described the same business chain configured on Odoo 19 Community.

The **business intent carries over essentially intact** — BOQ independence from the client price, one-way SITC explosion, the ≥3-vendor rule, the Purchase Manager's fully discretionary award, Site Engineer verification before cost is accepted, the Administrator-only post-approval override, and the Project Manager as sole owner of profitability.

Nine **structural** things changed, because building from scratch removes the constraints that forced the earlier shape. Several of those changes fix defects rather than merely relocating the design — most importantly a quantity ceiling that permanently lost headroom whenever material was returned, a race between three independently-validated document types, two contradictory mechanisms described for the same procurement step, and an entire category of site spend that could not structurally reach the profitability figure.

[Process Design §9](docs/01-process-design.md#9-what-changed-from-the-odoo-design-and-why) sets out all nine with the reasoning for each.

**The honest trade:** rev. 17 §2 judged that building this from scratch would take a large team many months. That was correct and still is. Roughly a year of engineering buys a process that fits Discern exactly, a schema Discern owns, and no vendor upgrade path — a legitimate trade, but not a cheaper one. [Build Plan §1](docs/04-build-plan.md#1-honest-effort-statement) states the effort plainly.

---

## Running it

Requires Docker, or a local PostgreSQL 16 and Redis.

```bash
cp .env.example .env
docker compose up --build      # or: make up
make migrate
make seed                      # demo login + a worked project from the real BOQs
make run                       # http://localhost:8000  —  demo / discern2026
```

Other targets: `make test` (192 tests), `make test-ceiling` (just the
invariant the design rests on), `make demo` (terminal walkthrough).

### Screens

| Screen | What you can do |
|---|---|
| **Dashboard** | Portfolio margin, per-project revenue/cost/margin, schedule alerts |
| **CRM** | Lead list with assignment, opportunity pipeline, follow-up due |
| **Sales** | Orders and lots; confirm an order, approve it for kickoff |
| **Projects** | Margin by SITC lot, cost by category, master schedule, record an extension, open the next BOQ revision |
| **BOQ** | Both sections with live committed/headroom per line, add lines, sign off, mark not applicable, release, send back, and the reconciliation verdicts |
| **Procurement** | Requests from all three sources, stock held elsewhere, RFQs, the comparison statement, discretionary award, purchase orders |
| **Receipts** | What is awaited, what the Store Keeper recorded, Site Engineer verification, discrepancies and returns |
| **Admin** | Users, roles, a capability matrix, project assignments, and the Administrator override log |

### Who can see what

Access is **role-based**. A role is a named set of permissions; a user holds one
or more roles. Permissions come in two kinds:

- **View** — opens a whole screen. Without it the nav item is hidden *and* the
  URL is refused. Hiding the link alone is not access control.
- **Action** — allows one operation inside a screen the user can otherwise see.
  Confirming an order and approving it for kickoff are separate permissions, so
  one person preparing an order cannot wave it through alone.

Ten default roles ship from the process design's role table (`make seed`), and
the capability catalogue lives in `apps/accounts/capabilities.py` — the admin
screen offers a checklist of what exists, so a typo cannot silently grant
nothing. Every role and assignment change is audited.

Every button calls the same domain service the tests exercise. A view never
writes a model directly, so the quantity ceiling, the schedule cap and
lock-on-approval hold in the UI exactly as they do in the tests.

Without Docker, point `POSTGRES_HOST` / `POSTGRES_USER` / `POSTGRES_DB` at a
local server and run `python -m pytest`.

---

## What is built: Phase 0

The three mechanisms nine of the ten business modules depend on. Built first
because building them afterwards means retrofitting each one nine times.

| Component | Where | What it guarantees |
|---|---|---|
| **Commitment ledger** | `services/ceiling.py` | One `reserve_headroom` under `SELECT FOR UPDATE`. Signed entries, so returns and cancellations net out. Per-category wastage tolerance; logged PM override |
| **Cost ledger** | `services/costing.py` | Append-only. Profitability and per-lot margin as queries. Corrections are reversals |
| **Stock ledger** | `services/stock.py` | Append-only moves; on-hand and cross-location availability derived |
| **Approval engine** | `models/approval.py`, `services/approvals.py` | Declarative rules, role enforcement, lock-on-approval in the domain layer, Administrator override with mandatory reason |
| **Event bus** | `services/events.py` | Transactional outbox, at-least-once delivery, retries, dead-letter queue with replay |
| **Access control** | `apps/accounts/` | Composable roles, capabilities, project-scoped querysets |

Append-only is enforced by a **database trigger**, not by revoked grants —
grants are bypassed by superusers and table owners, so a grant-based rule is
one `psql` session away from not being a rule. See
`apps/platform_core/migrations/0002_append_only_triggers.py`.

`apps/core/` holds deliberately thin `Project`, `BoqLine`, `Item` and
`Location` stubs — only as much as the ledgers must point at. Phases 1 and 2
extend them rather than rehoming every foreign key.

## What is built: Phase 1 — Sales to Project

The first slice that produces something usable, and the one that lands `Lot`.

| Module | Covers |
|---|---|
| **CRM** (`apps/crm`) | Leads with rule-based auto-assignment, opportunity pipeline including the site-visit and estimating stages, site visits, activities |
| **Sales** (`apps/sales`) | Clients, quotations, **lots** (itemized and lump-sum SITC), order confirmation with a committed delivery date, change orders, client invoicing |
| **Projects** (`apps/projects`) | Project initiation from a kicked-off order, master schedule with multi-stage procurement, the committed-date ceiling, client-agreed extensions |

Two things are worth looking at specifically:

**The kickoff gate and the hand-off.** A confirmed order sits in *held pending
review*; it does not create a project. When someone with the capability
approves it for kickoff, an `OrderApprovedForKickoff` event goes to the outbox,
and its handler creates the project — copying the client, budget, committed
date and every lot, and provisioning the project's own stock location. Nothing
is re-typed. Because outbox delivery is at-least-once, initiation is idempotent.

**The schedule ceiling.** No phase may be planned or rescheduled beyond the
project's `effective_committed_date`. Raising it requires a `ScheduleExtension`
carrying a mandatory client-agreement reference and CEO/PM authority. Every
date change is logged with its author and reason.

This is enforced in the domain service, not as a database CHECK constraint —
the rule spans two tables, which a row-level CHECK cannot express. The data
model document carried that error and has been corrected.

---

## What is built: Phase 2 — BOQ and the ceiling

| Module | Covers |
|---|---|
| **Engineering** (`apps/engineering`) | BOQ revisions, discipline-owned sections, section sign-off, PM release, the reconciliation engine, and an importer for Discern's own spreadsheet format |

**One revision, two sections.** The Design Manager signs off Goods, the
Construction Manager signs off Service, and either may be marked *not
applicable* when the project has no such scope — which is what stops a
materials-only project deadlocking on a signature nobody can give. Release is
the PM's single approval, and it locks the revision.

**The reconciliation engine** is the highest-risk item in the build plan. It
computes the net change per line against the **commitment and stock ledgers**,
not against the previous revision's text — because what the engineer changed
and what still needs doing are different numbers the moment an order is in
flight. Six outcomes, routed separately: request the delta, quietly reduce a
draft, amend an outstanding order, or queue a return.

It is tested against **Discern's real BOQ revisions** (`tests/fixtures/`) —
the LINAC Building fire protection Rev 0 and Rev 1. Using real documents rather
than synthetic ones surfaced three things worth having found:

- A description typed inconsistently within one nine-line table
  (`100 mm Nb` vs `80 mm NB`). Raw string matching would have read that as a
  deleted line plus an unrelated new one — a spurious return *and* a spurious
  purchase for a line nobody touched. Matching is normalised.
- Removals expressed as **quantity 0** with the row kept, not as deleted rows.
- New lines appended at the end so SL numbers stay stable.

`BoqLine.item` is nullable as a direct result: the real documents carry
SL NO / DESCRIPTION / UNIT / QTY, with no item-master reference and **no rate**.

---

### Test coverage of the invariants

131 tests, all passing against real PostgreSQL, and verified order-independent.
The ones that matter:

- **Property-based ceiling tests** — 150 randomized sequences of reserve,
  release, amend and cancel. After *every* operation: committed never exceeds
  the ceiling, never goes negative, and always equals what actually happened.
  Release everything and headroom returns to exactly the full ceiling.
- **Concurrency tests** — real threads on real connections. Two simultaneous
  reservations of 60 against a ceiling of 100 yield exactly one success; ten
  simultaneous reservations of 15 yield exactly six.
- **Named regression tests** for each defect the redesign set out to fix,
  including `test_return_releases_headroom_so_the_material_can_be_reordered`.
- **Append-only tests** that attempt raw SQL `UPDATE`/`DELETE` and confirm the
  trigger refuses them.
- **Hand-off tests** proving a kicked-off order produces a project with its
  lots, and that replaying the event does not produce a second one.
- **Schedule ceiling tests** covering plan, reschedule, block, extend, and the
  mandatory client agreement.
- **Per-lot margin tests** showing an order with two SITC lots reporting two
  margins rather than one blended figure.
- **Reconciliation tests against real BOQ revisions**, covering all six
  outcomes — including a partial receipt that splits one line into a return of
  what arrived and a cancellation of what had not yet shipped.

---

## What happens next

1. **Review the design** — start with the Process Design document.
2. **Settle the 8 blocking decisions** in the [Decisions Register](docs/05-decisions.md#tier-1--blocking-these-change-the-schema). Decisions #1 (wastage tolerance) and #2 (PM ceiling override) are implemented to the recommended defaults; changing them is configuration, not rework.
3. **Phase 3** — Procurement and receipt: vendors, procurement requests from all three sources, cross-location stock availability, RFQ to three vendors, the comparison statement, discretionary award, purchase orders against the ceiling, and Site Engineer verification before cost is accepted.

---

## Stack

PostgreSQL 16 · Python 3.12 · Django 5 · Django REST Framework · Celery + Redis · React 18 + TypeScript · Vite (frontend from Phase 1)

A modular monolith, not microservices — the commitment ledger and the documents that write to it must commit atomically, and the quantity ceiling is the strongest requirement in the design. See [Architecture §1](docs/02-architecture.md#1-shape-modular-monolith).
