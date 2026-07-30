# Build Plan

Companion to [`01-process-design.md`](01-process-design.md). This replaces rev. 17 §12, which was written before roughly half of that design existed and never updated to sequence it.

---

## 1. Honest Effort Statement

**This is a substantially larger undertaking than configuring an existing platform, and the plan should say so plainly before it says anything else.**

Rev. 17 §2 estimated that building this chain from scratch "would take a large team many months." That assessment was correct and it still applies. The trade being made is roughly a year of engineering in exchange for a process that fits Discern exactly, a schema Discern owns, and no vendor upgrade path to manage. That is a legitimate trade — it is simply not a cheaper one.

**Indicative effort, 3–4 engineers** (1 backend-heavy, 1 full-stack, 1 frontend, plus part-time QA):

| | Duration | Cumulative | Usable at the end? |
|---|---|---|---|
| Phase 0 — Platform foundations | 4–6 weeks | ~1.5 months | No — internal only |
| Phase 1 — Sales to Project | 6–8 weeks | ~3.5 months | **Yes** — replaces CRM/sales spreadsheets |
| Phase 2 — BOQ & the ceiling | 8–10 weeks | ~6 months | **Yes** — BOQ becomes the controlled document |
| Phase 3 — Procurement & receipt | 10–12 weeks | ~8.5 months | **Yes** — the core loop closes |
| Phase 4 — Subcontract & fabrication | 8–10 weeks | ~11 months | **Yes** — full chain live |
| Phase 5 — Costing, dashboards & site apps | 6–8 weeks | ~12.5 months | **Yes** — the payoff |

Treat these as planning ranges, not commitments. The two phases most likely to overrun are **2** (the reconciliation diff is subtler than it looks) and **3** (procurement has the most states and the most exceptions).

**Every phase from 1 onward ends with something Discern actually uses in production.** That is not a nicety — it is the only reliable way to find out whether the design matches how people actually work, while there is still budget to act on the answer.

---

## 2. Phase 0 — Platform Foundations
**4–6 weeks. Nothing user-visible. Do not skip and do not compress.**

Nine of the ten business modules depend on these three mechanisms. Building them afterwards means retrofitting each one nine times.

- [ ] Repo, CI, Docker Compose, migrations, linting, test harness
- [ ] `app_user`, `role`, `project_assignment`, capability checks, project-scoped base queryset
- [ ] **Audit trail** — actor/timestamp/before/after on every approval-carrying model
- [ ] **Approval engine** — declarative rules, request/action records, `locked_at`, domain-layer lock enforcement
- [ ] **Administrator override** — first-class object with mandatory reason, immutable log
- [ ] **Event bus** — transactional outbox, Celery drain, idempotency keys, retries, dead-letter queue with replay
- [ ] **Commitment ledger** + `reserve_headroom` / `release_headroom` with `SELECT FOR UPDATE`
- [ ] **Cost ledger** and **stock ledger** tables, append-only enforced by revoked `UPDATE`/`DELETE` grants
- [ ] **Property-based test suite on the ceiling** — randomized sequences of order / amend / cancel / return must never leave committed quantity above the BOQ figure, and must return headroom to exactly zero consumption when everything is cancelled
- [ ] Notification service, React shell, auth, layout, design system

**Exit criteria:** the ceiling property tests pass over thousands of generated sequences; an event survives a worker kill mid-handler and is delivered exactly once on restart; a locked record cannot be written through REST, admin, shell or a background job.

That last criterion is worth testing explicitly for all four paths. A lock that holds in the API and not in the admin screen is not a lock.

---

## 3. Phase 1 — Sales to Project
**6–8 weeks. First production use.**

- [ ] CRM: leads, assignment rules, opportunity pipeline with the site-visit/estimating stages, activities, site visit records
- [ ] Sales: client master, quotations, **lots** (itemized and lump-sum SITC), order confirmation, `committed_delivery_date`
- [ ] Change orders
- [ ] Kickoff approval gate → `OrderApprovedForKickoff`
- [ ] Project creation from order: cost scope, lots copied, site location provisioned
- [ ] Master schedule: phases, multi-stage procurement, the **committed-date ceiling constraint**, extension with recorded client agreement
- [ ] Client invoicing against lots → `REVENUE` cost entries

**Deliverable:** Sales runs entirely on the platform. Projects are created without re-typing. The schedule is capped at what the client was promised.

**Why `lot` and the cost ledger land in Phase 1:** both are structural. Retrofitting a lot reference onto BOQ lines, commitments and cost entries after they exist is a migration across the whole schema. Building it first costs almost nothing.

---

## 4. Phase 2 — BOQ & the Ceiling
**8–10 weeks. The design's centre of gravity.**

- [ ] Item master, categories, UoM
- [ ] `boq_revision` / `boq_section` / `boq_line` with mandatory lot and fixed route
- [ ] Discipline-owned sections, concurrent editing by Design and Construction Managers, mutual visibility
- [ ] `is_not_applicable` on empty sections
- [ ] Section sign-off, then Project Manager release; Director approval above threshold; send-back with comments
- [ ] `boq_line_link` line identity across revisions
- [ ] **Reconciliation engine** — diff against the commitment and stock ledgers, producing the five outcomes of process §4.5
- [ ] Ceiling wired into a stub authorizing document, so the ledger is proven before Procurement depends on it
- [ ] BOQ print, revision history, per-revision diff view

**Deliverable:** the BOQ is a controlled, revisable, approved document, and the ceiling is real.

**Highest-risk item in the whole build:** the reconciliation engine. Budget for it explicitly. Get the five outcomes of §4.5 under test before writing the UI, and get real historical BOQ revisions from Discern to test against — synthetic revisions will not exercise the messy cases.

---

## 5. Phase 3 — Procurement & Receipt
**10–12 weeks. The core loop closes.**

- [ ] Vendor master, empanelment, agreed rates
- [ ] `procurement_request` with all three sources, `is_site_raised` flag, PM approval for site requisitions
- [ ] **Cross-location stock availability** — on-hand by location, last purchase price and date
- [ ] Internal transfer as an alternative to purchase, at the Purchase Manager's discretion
- [ ] RFQ to ≥3 vendors, waiver with recorded reason
- [ ] **Comparison statement** — on screen and printable, best-in-class highlighted as information only
- [ ] **Award** — fully discretionary, `comparison_snapshot` frozen at award
- [ ] Purchase orders, ceiling check, threshold approval, amendments
- [ ] Expected receipts → goods receipt → **Site Engineer verification** → stock + `MATERIAL` cost
- [ ] Discrepancy handling, debit notes, replacement requests, headroom release
- [ ] Material returns with commitment net-down and cost reversal

**Deliverable:** the loop from released BOQ to verified material at site, with cost landing in the ledger automatically.

---

## 6. Phase 4 — Subcontract & Fabrication
**8–10 weeks. Full chain live.**

- [ ] Service orders direct from BOQ service lines with agreed rates; RFQ fallback for non-empanelled vendors
- [ ] Ceiling and threshold approval on service orders
- [ ] Progress logging by Project and Construction users
- [ ] **Certification** — partial and final, running-bill numbering → vendor bill → `SUBCONTRACT` cost
- [ ] Bills of materials, `fabrication_order` (in-house and job-work modes)
- [ ] Raw-material availability check; `FABRICATION_SHORTFALL` child requests
- [ ] Work steps, `material_consumption` planned vs. actual
- [ ] Completion → stock + `FABRICATION` cost
- [ ] Site expenses with approval → `SITE_EXPENSE` cost
- [ ] Excess/dead stock flag, three-dashboard fan-out, inter-project transfer with paired cost entries

**Deliverable:** all three fulfilment routes operational. Every rupee of project spend is in one ledger.

---

## 7. Phase 5 — Costing, Dashboards & Site Apps
**6–8 weeks. The reason for the whole exercise.**

- [ ] Profitability engine: planned vs. committed vs. received vs. billed, **per project and per lot**
- [ ] Project Manager dashboard (process §6.1)
- [ ] Construction Manager dashboard with Site Expense vs Income
- [ ] Purchase Manager dashboard: warehouse stock and value across all locations
- [ ] Directors' portfolio roll-up and **override review log**
- [ ] Drill-through from every figure to its entries and documents
- [ ] **Mobile site screens** — receipt, verification, progress, expenses, stock flagging; offline-tolerant
- [ ] Materialized views and query tuning for portfolio-scale reporting

**Deliverable:** live profitability per project and per lot, and site data entry that actually happens because the screens suit a phone at a site gate.

---

## 8. Sequencing Rules

Three constraints on any re-planning of the above:

1. **Phase 0 is not optional and not parallelizable.** The approval engine, commitment ledger and event bus are load-bearing for everything after.
2. **`lot` and the cost ledger belong in Phase 1**, even though nothing consumes them until Phase 2. Adding them later is a schema-wide migration.
3. **Nothing authorizes quantity before the ceiling exists.** If Procurement were built before Phase 2, it would ship without the design's central control and acquire it by retrofit — which is exactly how the earlier design ended up with three separately-validated document types and a race between them.

---

## 9. Migration & Cutover

- **Master data first** — clients, vendors, items, agreed rates. Cleanse before import, not after; a from-scratch build is the one chance to not inherit a decade of duplicate vendor records.
- **In-flight projects** — enter as an opening BOQ at Rev 1 with commitments and stock seeded to match reality on the cutover date. Do not attempt to replay history; there is no value in it and considerable risk.
- **Parallel run** — one live project on both the platform and the existing spreadsheets for one full procurement cycle. Reconcile the cost figures, and treat any discrepancy as a defect until proven otherwise.
- **Phased role onboarding** — Sales in Phase 1, Design and Construction Managers in Phase 2, Purchase and Store in Phase 3, site roles in Phase 4–5. Nobody is trained on a screen that will change before they use it in anger.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| **Reconciliation engine complexity** — the single most likely overrun | Test against real historical BOQ revisions from Discern, not synthetic data. Get the five outcomes under test before any UI |
| **Ceiling too rigid for site reality** — blocks legitimate work, gets worked around | Settle decisions #1 (tolerance) and #2 (PM override) *before* Phase 2. Monitor ceiling-block frequency from day one as a process signal |
| **Site data entry does not happen** — the whole cost picture depends on it | Mobile-first screens, built with actual site staff, tested at an actual site. If receipt and verification are painful, the ledgers are empty and the dashboards lie |
| **Scope creep into general ERP** — payroll, assets, statutory books | Process §1.1 draws the boundary. Hold it. Export to the accounting system; do not become one |
| **Team turnover over a 12-month build** | Documentation-as-you-go; the invariants under property tests; no undocumented tribal logic in the domain layer |
| **Design outruns the document, as rev. 17's §12 did** | These docs live in the repo and change in the same pull requests as the code |
