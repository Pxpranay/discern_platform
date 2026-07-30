# Discern Platform

Integrated operations platform for Discern Engineering Pvt Ltd — a purpose-built system covering the full chain from enquiry to project cost across multiple simultaneous construction/EPC projects.

```
Enquiry → Sales → Project → BOQ → Procurement → Fabrication / Subcontract → Receipt → Costing
```

**Status: design under review. No code yet.**

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
| 6 | [**Flowcharts**](docs/06-flowcharts.md) | Six focused diagrams, rendered inline by GitHub |

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

## What happens next

1. **Review the design** — start with the Process Design document.
2. **Settle the 8 blocking decisions** in the [Decisions Register](docs/05-decisions.md#tier-1--blocking-these-change-the-schema). Everything else has a recommended default and can wait for rollout.
3. **Phase 0** — platform foundations: approval engine, commitment ledger, event bus. Nine of the ten business modules depend on these three, so they are built first and are not compressible.

---

## Proposed stack

PostgreSQL 16 · Python 3.12 · Django 5 · Django REST Framework · Celery + Redis · React 18 + TypeScript · Vite

A modular monolith, not microservices — the commitment ledger and the documents that write to it must commit atomically, and the quantity ceiling is the strongest requirement in the design. See [Architecture §1](docs/02-architecture.md#1-shape-modular-monolith).
