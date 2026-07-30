# Discern Engineering — Integrated Operations Platform
## Process & System Design (Custom Build)

**Scope:** Enquiry → Sales → Project → BOQ → Procurement → Fabrication / Subcontract → Material Receipt → Costing
**Status:** Design for review. Not yet built.
**Supersedes:** *Discern CRM to Purchase Process Design* rev. 17 (Odoo-based). See §9 for what changed and why.

---

## 1. Purpose & Scope

Discern Engineering runs multiple construction/EPC projects simultaneously, each with its own client, site, budget, and material requirement. Information today is re-typed at every handoff: a lead becomes a quotation, a won job becomes a project, someone works out the materials, someone else raises purchase orders, someone at site receives material.

This document designs a **single purpose-built platform** covering that entire chain, where each stage feeds the next without re-entry, and where one question — *what is this project costing us right now, against what the client agreed to pay* — is always answerable as a live query rather than a month-end reconciliation.

Every project's cost, commitment, and stock position is derived from **append-only ledgers**, not from mutable summary fields. That single architectural choice is what makes the numbers trustworthy, and it is the main reason this design departs from the earlier one.

### 1.1 What this platform is not

It is not a general-purpose ERP. It is deliberately narrow: it models Discern's own chain, and nothing else. There is no payroll, no fixed-asset register, no general ledger. Financial output is a **cost and revenue position per project**, intended to be reconciled into whatever accounting system Discern keeps its statutory books in. Building it that way is what keeps a from-scratch build tractable.

---

## 2. Design Principles

These seven principles are the reason the rest of the document looks the way it does. Each one exists to prevent a specific class of failure that the process is otherwise prone to.

| # | Principle | What it prevents |
|---|---|---|
| 1 | **Ledgers, not counters.** Cost, committed quantity, and stock on hand are *derived* by summing append-only entries. No process ever increments a stored total. | Drift. A stored `received_qty` that a bug, a crash, or a concurrent write leaves wrong is wrong forever and nobody knows. A sum is always correct or provably not. |
| 2 | **Corrections are reversals.** Nothing that has been approved is ever edited or deleted. A change posts a compensating entry that carries its own reason and author. | Silent rewriting of history. Post-hoc edits that make a project's cost look different from what actually happened. |
| 3 | **One entry point per capability.** All three ways a purchase need arises produce the *same* record. All four documents that authorize quantity call the *same* function. | Divergent code paths. A rule enforced on three of four paths is not enforced. |
| 4 | **Approval is a platform service, not per-screen logic.** Every document type declares its approval rules; one engine enforces them, locks on approval, and audits overrides. | The failure mode named in the earlier design's §10 — some records lock hard, some only hide a button. |
| 5 | **Hand-offs are events, and events are durable.** Every automatic hand-off is a named domain event written in the same transaction as the change that caused it, then delivered by a worker with retries. | Lost hand-offs. A crash between "order confirmed" and "receipt expected" leaving the two permanently out of step. |
| 6 | **Project isolation by default, with named exceptions.** Cost and stock are scoped to one project unless a document explicitly and auditably crosses the boundary. | Cross-project cost bleed, which makes every project's profitability figure meaningless at once. |
| 7 | **Every derived number is drillable to its entries.** No figure appears on any dashboard without a path to the documents that produced it. | Numbers nobody trusts, and therefore nobody uses. |

---

## 3. Core Concepts

Eleven concepts carry the whole design. Everything else is a screen over one of these.

### 3.1 Project
The organizing unit. Owns a client, a site, a budget, a schedule, a BOQ, one or more stock locations, and a slice of the cost ledger. A project is created from a confirmed order and is the only thing cost is ever attributed to.

### 3.2 Lot
**A first-class entity between the order and the BOQ.** An order is a set of lots. A lot is one commercially-priced deliverable — either an itemized group of lines, or a single lump-sum **SITC** scope ("SITC of Fire Fighting System — ₹X"). Every BOQ line, every commitment, and every cost entry carries its lot.

Making the lot a real entity rather than a back-reference is what makes lot-by-lot margin a plain query. An order with "Lot 1: Fire Fighting SITC" and "Lot 2: HVAC SITC" shows margin separately for each, with no extra reporting work, because the lot was never optional metadata.

### 3.3 BOQ Revision
**One BOQ per project, revised as a whole.** A revision is an immutable snapshot of every line — Goods and Service together — with an author, a release date, and a computed diff against the previously released revision. Revisions are numbered (Rev 1, Rev 2, …) and never edited once released. The *current* BOQ is simply the latest released revision.

The BOQ is built from the project's drawings and physical site requirement. It is **independent of the order's price** in both directions: the BOQ may cost more or less than the client was quoted, and the quote never constrains what the BOQ may specify. Detail flows one way only — a lump-sum lot explodes down into BOQ lines; BOQ cost never flows up into price. Only a formal change order through Sales alters what the client owes.

### 3.4 BOQ Section
A revision contains **discipline-owned sections**, not separate documents:

- **Goods section** — owned by the Design Manager. Every material and fabricated item: steel, cement, aggregates, electricals, a custom MS staircase.
- **Service section** — owned by the Construction Manager. Subcontract and execution scope: civil work, plumbing, electrical installation.

Each owner signs off their own section. A section with no lines is **automatically complete** — a materials-only or labour-only project is not blocked waiting for a signature on an empty list. Both sections share one revision number, one diff, and one release.

### 3.5 Fulfilment Route
Every BOQ line carries exactly one route, fixed when the line is written and never re-decided downstream:

| Route | Meaning | Fulfilled by |
|---|---|---|
| `SUPPLY` | Standard material bought as-is and stocked | Purchase Order → Goods Receipt |
| `FABRICATE` | Made to the project's own drawings, in-house or job-worked | Fabrication Order → (child `SUPPLY` requests for missing raw material) → Production Receipt |
| `SERVICE` | Subcontracted execution scope | Service Order → Progress Certification → Vendor Bill |

Goods-section lines are `SUPPLY` or `FABRICATE`; Service-section lines are `SERVICE` by construction. A Purchase Officer opening a released BOQ can therefore see at a glance which lines are subcontract scope, which must be fabricated, and which are simply sourced.

### 3.6 Commitment Ledger — the quantity ceiling
**The single mechanism preventing over-authorization.** Every document that authorizes quantity against a BOQ line writes a signed entry:

```
Commitment(boq_line, document, qty_delta, actor, reason, created_at)
```

- Issue a PO for 100 → `+100`
- Amend it to 80 → `−20`
- Cancel it → `−100`
- Return 10 already received → `−10`

**Headroom = BOQ line quantity − SUM(qty_delta).** Returns, cancellations and amendments net out arithmetically, because releasing headroom is the same operation as consuming it with the opposite sign.

Every route calls one function, `reserve_headroom(boq_line, qty, document)`, inside the caller's own database transaction, taking a row lock on the BOQ line. Two buyers confirming simultaneously cannot both pass — the second waits and then sees the first's commitment. A fifth document type added in three years gets the ceiling for free by calling the same function.

The only way to raise a ceiling is a new BOQ revision.

### 3.7 Cost Ledger
Append-only, per project, per lot. Every cost- or revenue-bearing event posts an entry:

```
CostEntry(project, lot, boq_line?, category, amount, source_document, effective_date, actor)
```

Categories: `MATERIAL`, `FABRICATION`, `SUBCONTRACT`, `SITE_EXPENSE`, `STOCK_IN`, `STOCK_OUT`, `REVENUE`.

Profitability is a `GROUP BY` over this table — by project, by lot, by category, by period, with no separate reporting pipeline. Site expenses and subcontract bills land in the same ledger as material costs, so there is no category of spend that quietly fails to reach the profitability figure.

### 3.8 Stock Ledger
Append-only stock moves between locations. On-hand at any location is the sum of its moves. Each project's site is its own location; a central yard is another. Cross-warehouse availability is therefore an ordinary query, not a joined report assembled per request.

### 3.9 Approval Engine
Declarative rules per document type: *who* must approve, *under what condition*, *above what value*. The engine enforces the rule, records who approved and when, and **locks the document on approval**. After that, the only routes forward are a new revision, a formal amendment document, or an Administrator override — which is permitted, logged immutably, and surfaced to the Directors after the fact.

### 3.10 Event Bus
Named domain events written in the same transaction as the change that raised them, then delivered by a worker with idempotency keys, bounded retries, and a dead-letter queue an Administrator can see and replay. Every automatic hand-off in §4 is one of these.

### 3.11 Master Schedule
Named phases with target dates — Site Visit, BOQ Preparation, Procurement (as many stages as phased delivery needs), Construction. Every date is live and editable, every change is logged with its author. The schedule's hard boundary is the **order's committed delivery date**: no phase may be planned beyond it. Raising that ceiling requires a recorded client agreement and CEO/PM authorization.

---

## 4. The Process, Stage by Stage

### 4.1 Enquiry → Opportunity

A lead arrives (site enquiry, referral, tender notice) and enters the pipeline. Construction work typically needs a site visit and a rough-order technical estimate before it is a firm opportunity, so the pipeline carries an explicit **Site Visit / Technical Estimate** stage. Leads are auto-assigned by territory or work type, and follow-up activities are generated on a schedule.

**Gate:** an opportunity becomes quotable only when marked Won. Lost opportunities record a reason; unqualified ones return to nurture.

### 4.2 Quotation → Confirmed Order

Sales prepares a quotation as a set of **lots** (§3.2). A lot is either itemized or a single lump-sum SITC line. On client acceptance the quotation is confirmed as an Order, which fixes three things that matter downstream: the **agreed price per lot**, the **committed delivery date** (which caps the schedule, §4.3), and the **scope** each lot represents.

Change orders are the only mechanism that alters an order after confirmation. They are their own document, approved through Sales, and they adjust lot price and/or the committed date. They do not touch the BOQ.

**Gate:** a confirmed order does not create a project automatically. A Project Manager or Sales Manager marks it **approved for kickoff**. Until then it sits in *Held Pending Review* and is re-checked, rather than silently proceeding.

> **`OrderApprovedForKickoff`** → creates the Project, its cost-ledger scope, and its site stock location. Lots are copied to the project. Nothing is re-typed.

### 4.3 Project Initiation & Master Schedule

The project now exists with its client, site, budget, lots, and stock location. Before any BOQ or procurement work begins, the **CEO or Project Manager plans the master schedule** (§3.11) — one target date per phase, with procurement broken into as many stages as the site's phased delivery needs (foundation materials first, structural steel later, finishing materials last).

**Check:** does the plan fall within the order's committed delivery date?

- **Yes** → the schedule is set and work proceeds.
- **No** → blocked. The schedule cannot exceed the committed date. If the client agrees to a later delivery, the CEO/PM records that agreement, which raises the ceiling and reopens every phase for re-planning against the new date. The check then runs again.

Schedule status flows continuously to the Project Manager's dashboard.

### 4.4 BOQ Preparation

The Design Manager and the Construction Manager work **concurrently on one BOQ revision**, each in their own section (§3.4), each against the project's drawings and site conditions.

- The **Design Manager** writes Goods lines and tags each `SUPPLY` or `FABRICATE`.
- The **Construction Manager** writes Service lines.

Every line carries its **lot**. This is where a lump-sum SITC lot gets its detail filled in: the client's single line says nothing about which pumps, pipes, cables, fabricated items or man-days are needed, so the Design Manager works out the materials and fabricated items and the Construction Manager works out the labour and subcontract scope. One lot line commonly explodes into dozens of BOQ lines across both sections. Because every line carries its lot, that lump-sum price stays traceable to its actual cost.

Both managers can see the whole revision while preparing it — including each other's section — so a scope item is not double-costed as both material and labour, and an unclear labour/material split is visible while it can still be resolved.

Drawings change, site conditions differ from the estimate, clients revise scope. A revision may therefore be prepared any number of times over a project's life, each going through §4.5 before it releases. Every past revision is retained in full.

### 4.5 Release: Approval & Reconciliation

**Section sign-off.** Each owner confirms their own section is complete and ready. Empty sections are automatically complete (§3.4).

**Project release.** Once both sections are signed off, the **Project Manager** gives the single approval that releases the revision. Above a configurable value threshold this additionally requires a Director. Sending the revision back returns it to preparation with comments; nothing downstream has moved.

> **`BoqRevisionReleased`** → the **reconciliation engine** runs.

The reconciliation engine does not re-send the BOQ. It compares the released revision against **what has actually been committed and received so far** — read from the commitment and stock ledgers, not from the previous revision's text — and computes the true net change per line:

| Net change | Outcome |
|---|---|
| **New line, or quantity increased** | The **delta only** becomes a Procurement Request (§4.6). Only the incremental amount is ever requested. |
| **Quantity decreased, nothing committed yet** | Any still-draft request line is reduced or removed. No vendor has been contacted; nobody is notified. The quiet outcome. |
| **Quantity decreased, already ordered but not delivered** | Purchase amends or cancels the outstanding quantity on the open order. The commitment ledger nets down automatically (§3.6). |
| **Quantity decreased, already received** | Enters the **Return / Redeployment queue**: return to vendor with a debit note, or redeploy the surplus to another project that needs it (§4.12). Either way the commitment nets down and the cost ledger follows. |
| **Line unchanged** | Nothing happens. |

Because reconciliation reads the ledgers rather than the prior revision, it stays correct even when a revision lands while orders are mid-flight — which, on a live project, is the normal case rather than the exception.

### 4.6 Procurement Requests — three sources, one record

Every purchase need becomes the **same** `ProcurementRequest`, distinguished only by a `source` field:

| Source | Raised by | Gate |
|---|---|---|
| `BOQ_RELEASE` | Reconciliation engine, automatically | Already approved via §4.5 |
| `SITE_REQUISITION` | Construction team, from what they see on site | **Project Manager approval required** before Procurement sees it |
| `FABRICATION_SHORTFALL` | Fabrication Order short of raw material | Already approved via the parent order |

A **site requisition** exists so the Construction team need not wait for the next BOQ cycle when they can see the requirement on the ground. It is bound by the same ceiling as every other route: it draws against a specific BOQ line and cannot exceed its headroom. A *Hold* outcome parks it for re-review rather than discarding it; once approved it joins the normal flow from the top. Requests remain flagged `site-raised` throughout Procurement, so buyers know a request came from an on-site call rather than a scheduled cycle.

Downstream logic is written once, against one record shape.

### 4.7 Sourcing

**Stock availability first.** Before any RFQ leaves the building, the Purchase Manager sees, for each requested item: on-hand quantity **in every location Discern operates**, plus last purchase price and last purchase date. This is a query over the stock and purchase ledgers, always live.

The Purchase Manager may **divert the requirement to an internal transfer** from a location that already holds the stock, or buy new regardless. The platform surfaces the information; it does not force either choice.

**Multi-vendor RFQ.** Each line goes to **at least three vendors**. An RFQ cannot advance with fewer than three responses unless the Purchase Manager records a reason (a specialised item with only one or two capable suppliers is a real situation, and blocking it indefinitely would be worse than recording why).

**Comparison statement.** Quotes are presented side by side — price, unit price, delivery date, vendor terms — with the best in each dimension highlighted **as information only**. The statement is printable and attaches to the approval record.

**Award.** The Purchase Manager selects the winning vendor **entirely at their own discretion, irrespective of quoted price**. Nothing in the platform auto-selects, defaults to lowest, or requires justification to award elsewhere. The award is recorded with its comparison statement, which is the audit trail.

**Purchase Order.** The order is raised against the awarded vendor, tagged to the project and lot.

- `reserve_headroom` is called. Beyond the ceiling, the order is **blocked outright** with a clear reason and a pointer to the BOQ line — not accepted with a warning.
- Above a configurable value the order requires **Purchase Manager approval** before it is live.

> **`PurchaseOrderConfirmed`** → an expected receipt is created at the project's site location, for `SUPPLY` lines only.

### 4.8 Fulfilment Routes

The route fixed at BOQ stage (§3.5) now determines what happens, with no further decision:

**`SUPPLY`** → §4.9.

**`FABRICATE`** → a **Fabrication Order** is created against the item's bill of materials, capped by `reserve_headroom` on the finished item. Raw-material availability is checked against the stock ledger:

- Available → work steps proceed to production.
- Short → child Procurement Requests are raised for the missing raw materials only (`FABRICATION_SHORTFALL`, §4.6), which flow through §4.7–4.9 like any other purchase and release the fabrication order on arrival.

Raw-material requests are **not** ceiling-checked against a BOQ line, deliberately: they are not buying the BOQ line's item, they are buying components consumed to produce it. The finished quantity was already capped before the fabrication order existed, so the ceiling sits upstream. Raw-material consumption is still recorded against the order, so a fabrication run that consumes materially more than its BOM is visible rather than absorbed.

On completion, the finished item enters stock at the project's location and posts `FABRICATION` cost. Downstream costing neither knows nor cares whether an item was bought or built.

**`SERVICE`** → §4.10.

### 4.9 Material Receipt

Material arrives at the project site, or at a central yard for onward delivery. The Store/Site Keeper records the receipt against the expected quantity.

**Gate:** a **Site Engineer verifies quantity and quality** against the order before the receipt is accepted.

- **Match** → stock posts to the project's location; `MATERIAL` cost posts to the ledger against the project, lot and BOQ line; the vendor bill is cleared for payment.
- **Mismatch** → a discrepancy is logged with quantities and photographs, driving a vendor debit note and, where needed, a replacement request. The shortfall's headroom is released so the replacement can be ordered. **Nothing enters the project's cost on the strength of an unverified delivery.**

Partial receipts are normal; each posts independently and the remainder stays outstanding.

### 4.10 Subcontract Execution

Subcontracted work does not go out to tender the way material does. Discern has empanelled subcontractors and agreed rates for most trades, so a **Service Order** goes direct: scope, quantities and agreed price pulled from the BOQ Service line, vendor pre-selected, no RFQ round.

- `reserve_headroom` applies identically.
- Above the configured value the order still needs **Purchase Manager approval** — a directly-issued service order is not a route around the approval threshold.
- New or one-off subcontractors without an agreed rate route through the normal §4.7 RFQ instead.

**Progress monitoring** is the part a purchase order screen cannot give. Against the BOQ scope it was raised from, the platform tracks percentage complete, running-bill quantities certified to date, and what remains outstanding. Both Project users and Construction users — including site coordinators who see the work daily — can log progress.

**Gate:** completion is **certified** (in full or in part) before billing. On certification a vendor bill is raised directly against the service line and posts `SUBCONTRACT` cost. There is no goods receipt, because there is nothing physical to receive. Running-bill certification is expected and supported: a subcontractor bills progressively, and each certified stage bills independently.

### 4.11 Site Expenses & Client Invoicing

A site carries running costs outside the BOQ entirely that still consume margin: **room rent, water, site conveyance, site fooding, site miscellaneous**. Site staff log these against the project; they post `SITE_EXPENSE` to the same cost ledger as everything else.

Client invoices are raised against lots per the order's payment terms and post `REVENUE`, dated.

Because expenses, subcontract bills, material costs and revenue all share one ledger, the **Site Expense vs Income** view is a query over it — dated, per project, per lot — not a separate report to assemble. It appears on the Construction Manager's screen and on the Project Manager's dashboard: **one query, two viewers, identical numbers**.

### 4.12 Dead & Excess Stock Redeployment

Site conditions change after material has been delivered. A design revision, an over-estimated quantity, or scoped-down work leaves stock sitting at one site that another running project may need.

A **Site Engineer or Site In-Charge may flag any received line, at any time**, as dead stock or available for another project. The flag immediately reaches three dashboards — the Project Manager's, the Construction Manager's, and the Purchase Manager's — with item, quantity, project and location, so whoever is best placed to act sees it at once rather than at a routine review.

Where it results in redeployment, an internal transfer moves the stock, and the transfer posts **two cost entries**: `STOCK_OUT` crediting the releasing project, `STOCK_IN` debiting the receiving one — or a shared warehouse scope if the stock is going to sit unassigned. Both projects' profitability reflects the movement the same day.

This is the one deliberate breach of project isolation (§5.4). It is why every such transfer carries its own explicit paired cost entries rather than being treated as a movement with no cost consequence.

### 4.13 Costing & Profitability

The **Project Manager is the single named owner of a project's profitability.** Every input to that number therefore lives on one dashboard (§6.1) rather than scattered across modules.

Because every entry is tagged with project, lot, category and source document, the same underlying data answers all of:

- Planned BOQ cost vs. committed vs. received vs. billed, per line
- Margin per project — and **per lot**, so an order with four SITC lots shows four margins, not one blended total
- Cost by category, so the weight of subcontract vs. material vs. site running costs is visible
- Cost over time against the master schedule
- Portfolio roll-up across every active project

Every figure drills to its entries, and every entry to its document.

---

## 5. Cross-Cutting Mechanisms

### 5.1 The Quantity Ceiling

**Requirement:** the platform must refuse to let the quantity ordered, fabricated or subcontracted against any BOQ line exceed what the latest released revision specifies — whether the excess comes from automation or from someone typing a bigger number.

The commitment ledger (§3.6) enforces this at the one place a quantity can change, for all four authorizing document types, under a row lock. Beyond the headroom — even by one unit — the document is blocked with a clear reason. The only way past is a new revision.

Two policy questions this raises are for Discern to decide, not for the platform to assume (§8): whether a small **wastage tolerance** applies to consumable materials like cement and tiles, and whether the Project Manager gets a **logged emergency override** for genuine site emergencies where there is no time to run a revision.

### 5.2 Governance: Undo Until Approval, Then Locked

Any record — a BOQ revision, a procurement request, an RFQ, a service order, a schedule phase — is **freely editable and reversible right up until it is approved and forwarded**. Nothing has committed downstream, so nothing is at risk.

The moment approval fires, the record **locks**. From then on:

- A BOQ changes only through a new revision.
- A purchase or service order changes only through a formal amendment document.
- Anything else changes only through an **Administrator override**, which is permitted, recorded immutably with actor, timestamp, before/after values and a mandatory reason, and surfaced to the Directors after the fact.

This is enforced in the domain layer, not by hiding buttons. A locked record cannot be written through any path — API, admin screen, or background job. Ledger entries are never locked *against* posting, because posting to a ledger is an append, not an edit; it is edits and deletes that are refused.

### 5.3 Automatic Hand-offs

Each hand-off below is one durable event with one handler. This is what removes re-typing.

| Event | Consequence |
|---|---|
| `OpportunityWon` | Quotation drafted from the opportunity's scope |
| `OrderApprovedForKickoff` | Project, cost scope, lots and site location created |
| `BoqRevisionReleased` | Reconciliation runs; deltas become Procurement Requests |
| `ProcurementRequestApproved` | RFQ drafted, grouped by preferred vendor |
| `PurchaseOrderConfirmed` | Commitment posted; expected receipt created (`SUPPLY` only) |
| `FabricationOrderCreated` | Commitment posted; raw material checked; shortfall requests raised |
| `ReceiptVerified` | Stock posted; `MATERIAL` cost posted; BOQ received-quantity updated |
| `FabricationCompleted` | Stock posted; `FABRICATION` cost posted |
| `ServiceProgressCertified` | Vendor bill raised; `SUBCONTRACT` cost posted |
| `ExpenseApproved` | `SITE_EXPENSE` cost posted |
| `InvoiceIssued` | `REVENUE` posted |
| `StockFlaggedExcess` | Three dashboards notified |
| `StockTransferred` | Paired `STOCK_OUT` / `STOCK_IN` cost entries posted |
| `OrderAmended` / `OrderCancelled` / `MaterialReturned` | Commitment nets down; cost reversal posted |

### 5.4 Project Isolation

Two mechanisms keep concurrent projects from bleeding into each other:

- **Cost scoping.** Every cost entry carries a project. "What has Project X cost" is a live query, never a reconciliation.
- **Location scoping.** Each site is its own stock location. Material received for Project X is not consumable by Project Y's requests.

The **one deliberate exception** is the inter-project transfer of §4.12, which crosses the boundary on purpose — which is exactly why it must carry its own paired cost entries.

### 5.5 Roles & Access

Access is **role-based, scoped per project**. A role grants capabilities on document types; a project assignment determines which projects those capabilities reach. A Construction User assigned to two projects sees those two, not the portfolio.

Roles are composable — one person may hold Purchase Manager and cover Construction duties on a smaller project — because forcing exactly one role per user does not survive contact with a real organization chart. Every role assignment is itself audited.

Site roles (Store Keeper, Site Engineer) get **mobile-first screens** covering receipt, verification, progress logging, expense capture and stock flagging. These are the highest-volume, lowest-patience interactions in the whole system, and a desktop back-office screen on a phone at a site gate is how data entry stops happening.

---

## 6. Dashboards

Each dashboard is a set of queries over the ledgers. None holds its own data, so no two disagree.

### 6.1 Project Manager
BOQ status (current revision, approval state, value) · site progress rolled up across service orders · purchase order movement (committed vs. received vs. billed) · master schedule status against the committed date · **profitability, per project and per lot** · excess-stock alerts for the project. Every tile drills through.

### 6.2 Construction Manager
Service BOQ section status · service order progress and certifications outstanding · **Site Expense vs Income** (the same query as §6.1, same numbers) · excess-stock alerts.

### 6.3 Purchase Manager
**Warehouse stock and value across every location**, kept live rather than pulled up per purchase · RFQs awaiting comparison · orders awaiting approval · excess-stock alerts, since redeploying rather than buying is this role's core lever · last purchase price and date per item.

### 6.4 Directors / Finance
Portfolio roll-up across all active projects · margin by project and lot · schedule exposure against committed dates · **an override log** of every Administrator action taken on a locked record.

---

## 7. Module Map

A modular monolith (§ architecture doc). Boundaries are enforced in code; the deployment is one application.

| Module | Owns |
|---|---|
| **Identity & Access** | Users, roles, project assignments, audit trail |
| **CRM** | Leads, opportunities, pipeline, activities |
| **Sales** | Quotations, orders, lots, change orders, client invoices |
| **Projects** | Project, master schedule, dashboards |
| **Engineering** | BOQ revisions, sections, lines, routes, section sign-off |
| **Procurement** | Procurement requests, RFQs, comparison, awards, purchase orders, vendors |
| **Fabrication** | Bills of materials, fabrication orders, work steps |
| **Subcontracts** | Service orders, progress, certification, running bills |
| **Inventory** | Locations, stock ledger, receipts, verification, transfers, excess flags |
| **Finance** | Vendor bills, site expenses, cost ledger, revenue |
| **Platform** | Approval engine, commitment ledger, event bus, notifications, reporting, audit |

**Platform is built first.** The approval engine, commitment ledger and event bus are used by nine of the ten business modules; building them after the modules means retrofitting the same three mechanisms nine times.

---

## 8. Roles Along the Chain

| Role | Responsibility |
|---|---|
| **Sales Representative** | Runs leads, opportunities and quotations |
| **Sales Manager** | Approves confirmed orders for project kickoff |
| **CEO** | Plans the master schedule with the PM; jointly the only role able to authorize extending it beyond the committed date once the client agrees |
| **Project Manager** | **Sole owner of project profitability.** Releases BOQ revisions; approves site requisitions; plans and extends the master schedule with the CEO; monitors BOQ status, site progress, purchase movement and expense-vs-income on one dashboard |
| **Design Manager** | Prepares, revises and signs off the BOQ **Goods section**; tags each line `SUPPLY` or `FABRICATE`; finalizes materials behind each SITC lot |
| **Construction Manager** | Prepares, revises and signs off the BOQ **Service section**; finalizes labour scope behind each SITC lot; raises and oversees service orders; monitors expense-vs-income |
| **Procurement Officer** | Sends RFQs to at least three vendors; prepares comparison statements; raises orders; actions reconciliation deltas and amendments. Cannot exceed the ceiling |
| **Purchase Manager** | **Awards every line at their own discretion, irrespective of price.** Approves orders above threshold; decides on existing stock vs. fresh purchase; owns the cross-warehouse stock and value dashboard |
| **Fabrication Supervisor** | Runs fabrication orders and work steps; confirms raw-material consumption and finished output. Cannot exceed the ceiling |
| **Construction User** | Raises service orders from released BOQ Service lines; logs daily progress; raises site requisitions for PM approval. Cannot exceed the ceiling |
| **Store / Site Keeper** | Records material receipt; raises returns for received excess |
| **Site Engineer / Site In-Charge** | **Verifies quantity and quality before any receipt is accepted;** certifies service completion; flags dead or excess stock at any time |
| **Finance** | Vendor bills, client invoices, expense approval, reconciliation to statutory books |
| **Directors** | Portfolio profitability; review of Administrator overrides |
| **Administrator** | The only role able to edit or reverse a record after approval — every action logged immutably and surfaced to the Directors |

---

## 9. What Changed From the Odoo Design, and Why

The earlier document (rev. 17) described the same business chain on Odoo 19 Community. The business intent is carried over essentially intact. Nine structural things changed, each because building from scratch removes the constraint that forced the earlier shape.

| # | Rev. 17 | This design | Why |
|---|---|---|---|
| 1 | **Two BOQs** in two modules, two module-level approvals, then a combined PM approval | **One BOQ revision** with two discipline-owned sections; empty sections auto-complete | Ownership was the goal and is fully preserved. The two-document split cost two revision streams that could drift, duplicated revision/approval/ceiling mechanics, and **deadlocked on any project with no service or no goods scope** — the earlier design had no "not applicable" state |
| 2 | Ceiling = sum over non-cancelled order lines, validated per document type | **Commitment ledger**, one signed entry per authorization, one `reserve_headroom` under row lock | Two independent fixes. The earlier sum **never released headroom on a return** — a returned line's order quantity stays intact, so the headroom was lost permanently and re-ordering was blocked forever. And three separate validations had a **race**: two buyers confirming at once both see headroom and both pass |
| 3 | §6.2 (custom automation drafts requisitions) and §6.8 (native routes fire automatically) described **two different mechanisms for the same line** | One reconciliation engine → one `ProcurementRequest`, `source`-tagged, three producers | The contradiction was unresolvable in Odoo without picking one. Here there is one path, and the fabrication-shortfall case is a producer of the same record rather than a parallel mechanism |
| 4 | Lock-on-approval via record rules | Domain-layer locking; ledger posting is an append, not an edit | The record-rule approach would have **blocked the received-quantity write-back and the committed-quantity field** on approved BOQ lines, breaking goods receipt until every sync ran with elevated privilege. Ledgers make the problem vanish: nothing writes back to a locked record |
| 5 | 19 bespoke automation rules | Durable event bus with idempotency, retries, dead-letter queue | Nothing in the earlier design said what happens when a hand-off fails halfway. At this many hand-offs, some will |
| 6 | BOQ line carries a **back-reference** to the order line, optionally | **Lot is a first-class entity** carried by every BOQ line, commitment and cost entry | Lot margin was described as "simply a different grouping" contingent on an optional field. Making the lot real makes it structural — and removes the open question of whether the link should be mandatory |
| 7 | Cost via analytic accounts; **site expenses could not reach the profitability panel** (§6.14 confirmed no expense reference exists in it), needing a separate combined report | One cost ledger; expenses, bills, fabrication, transfers and revenue all post to it | An entire category of real spend was structurally unable to reach the profitability figure. Site running costs consume margin and must be in the same number |
| 8 | Cross-warehouse availability and warehouse stock/value were **custom joined reports** over native data | Ordinary queries over the stock ledger | These were only hard because the underlying schema was not ours |
| 9 | §12 four-phase rollout, written before §6.9–6.19 existed and never updated — no mention of the Construction Module, Design Module, ceiling, schedule, dashboards or dead stock | Rewritten build plan (see build plan doc) | The plan had fallen well behind the design it was meant to sequence |

**Also carried forward deliberately, unchanged:** BOQ independence from the order price in both directions; one-way explosion of SITC detail; the ≥3-vendor rule; the Purchase Manager's fully discretionary award with no lowest-price default; native-equivalent double approval above a threshold; Site Engineer verification before cost is accepted; the deliberate absence of a ceiling check on fabrication raw materials; the Administrator-only post-approval override; and the Project Manager as the single named owner of profitability.

**One caveat inherited and now resolved:** rev. §6.18 flagged, honestly, that it could not confirm whether stock transfers natively carry cost between projects, because the module holding that logic was missing from its review checkout. On a bespoke schema the question does not arise — §4.12 posts paired cost entries explicitly, because we are writing that behaviour rather than inferring it.

---

## 10. Decisions Needed Before Build

The earlier document ended with 39 open questions, which is too many to gate a build. Only these **eight change the data model** and are genuinely blocking. Everything else is configuration that can be decided during rollout without holding up a line of code — see the decisions document for the full deferred list and a recommended default for each.

| # | Decision | Why it blocks |
|---|---|---|
| 1 | Does the ceiling allow a **wastage tolerance** (e.g. 2–5% on cement, tiles), or is every unit above the BOQ figure a formal revision? | Changes the headroom calculation and whether tolerance is per-item, per-category or global |
| 2 | Does the PM get a **logged emergency override** of the ceiling, reconciled into the BOQ afterward? | Adds an authorization path and a reconciliation obligation |
| 3 | Is fabrication **in-house, job-worked, or both**? | Job work means materials issued out and a fabricated item received back — a different stock and costing flow |
| 4 | What **value basis** for inter-project stock transfer: original purchase cost, current replacement cost, or Purchase-Manager-set? | Determines whether stock valuation must be tracked per receipt lot |
| 5 | Is each site a **separate warehouse or a location under a central yard**? | Shapes the location hierarchy and yard-to-site transfer tracking |
| 6 | **Section sign-off** — is the preparing Manager's own confirmation sufficient, or does a separate Design/Construction Head sign off? | Changes states and roles on the BOQ section |
| 7 | Does the platform need **multi-currency or import procurement**? | Retrofitting currency into a cost ledger is expensive; designing for it upfront is cheap |
| 8 | Which existing systems must this **integrate with** — statutory accounting, GST filing, banking? | Determines the export boundary of §1.1 and whether an integration module is Phase 1 or Phase 4 |

Two of rev. 17's questions are already answered structurally and need no decision: **BOQ revisions** are always multiple (§3.3), and the **BOQ-to-lot link** is always mandatory (§3.2).
