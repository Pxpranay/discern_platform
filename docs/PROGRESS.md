# Progress Log

**Purpose:** so a new session can pick up cheaply without re-reading the whole
codebase or the whole conversation. Read this file first; it is kept current
with every commit.

---

## State

| | |
|---|---|
| **Built** | Phases 0–5 — the full build plan, plus web app, admin/RBAC and mobile site screens |
| **Tests** | 264 passing against real PostgreSQL, order-independent |
| **Stack** | PostgreSQL 16 · Django 5 · server-rendered templates. **Settled — staying as is.** |
| **Login** | `demo` / `discern2026` — one command: `make up` → http://localhost:8000 |
| **Running it** | `docs/RUNNING.md`. `manage.py bootstrap` migrates, seeds, and loads the demo project only when the DB has no project |
| **Push** | Blocked — session GitHub token is read-only. Work is delivered as `git am` patches |

## Done

- **Phase 0 — platform foundations.** Commitment ledger (the BOQ quantity
  ceiling, `SELECT FOR UPDATE`, signed entries so returns net out), cost ledger,
  stock ledger, approval engine with lock-on-approval, transactional outbox
  event bus, role/capability access control. Append-only enforced by database
  trigger, not grants.
- **Phase 1 — Sales to Project.** CRM leads and pipeline; clients, quotations,
  **Lot** as a first-class entity (itemized and lump-sum SITC), orders with a
  committed delivery date, change orders, invoicing. Kickoff gate → project
  created automatically with lots and site location. Master schedule capped at
  the committed date, extension requires a recorded client agreement.
- **Phase 2 — BOQ.** One revision, two discipline-owned sections, sign-off,
  *not applicable* for an empty section, PM release, and the **reconciliation
  engine** — computed against the ledgers, not the previous revision's text.
  Tested against Discern's real LINAC Building Rev 0 → Rev 1.
- **Web app.** Login, nav, and a screen per module: Dashboard, CRM, Sales,
  Projects, BOQ. Every button calls a domain service.
- **Admin / RBAC.** Users, roles, capability matrix, project assignments.
  Views and nav gated by capability.
- **Phase 3 — Procurement and receipt.** Vendors and agreed rates; one
  `ProcurementRequest` for all three sources; cross-location stock availability
  and last purchase price before any RFQ; RFQ to ≥3 vendors with a recorded
  waiver where three is impossible; comparison statement marking best price as
  information only; fully discretionary award with the comparison frozen onto
  it; purchase orders consuming the BOQ ceiling, parked above a value
  threshold; goods receipt, **Site Engineer verification before cost is
  accepted**, discrepancies holding the vendor bill, and returns that release
  headroom so material can be re-ordered.

- **Phase 4 — Works, expenses, redeployment.** Bills of materials and
  fabrication orders in both in-house and job-work modes; raw-material
  shortfall raising child procurement requests; service orders direct to
  empanelled subcontractors on agreed rates, with progress logging separate
  from certification and running-bill billing; the five site-expense
  categories; dead/excess stock flagging with three-dashboard fan-out,
  receiving-PM acceptance and paired cost entries at original purchase cost.

- **Phase 5 — Dashboards and mobile.** The Project Manager's single dashboard
  (BOQ status, site progress, purchase movement, schedule, profitability, margin
  by lot), the Construction Manager's expense-vs-income sheet, the Purchase
  Manager's cross-location stock and value view, the Directors' portfolio
  sorted worst-margin-first with the override log and dead-letter queue, and
  mobile-first site screens for receipt, verification, progress and expenses.

## Next

**The build plan is complete.** What remains is not a phase but a decision and
a rollout:

1. **Settle the stack question.** Supabase-native versus Supabase-as-Postgres
   keeping the Python core. The app's shape is now concrete enough to decide on.
2. **Blocking decisions 5–8** — warehouse vs location per site, section sign-off
   level, multi-currency, integration boundary.
3. **Migration and cutover** (build plan §9): master data first, in-flight
   projects as an opening BOQ revision, then one project run in parallel for a
   full procurement cycle with the cost figures reconciled.
4. **Hardening for production**: real authentication policy, backups with
   rehearsed restores, observability on outbox lag and ceiling-block frequency.

## Decisions taken

| # | Decision | Where |
|---|---|---|
| 1 | Wastage tolerance per item category, default 0% | `core.ItemCategory.wastage_tolerance_pct` |
| 2 | PM emergency ceiling override, logged | `ceiling.reserve_headroom(override_actor=…)` |
| 3 | Both fabrication modes — in-house and job work | `fabrication.FabricationOrder.mode` |
| 4 | Transfer valued at original purchase cost | `stock.valuation_at`, `ExcessStockFlag.unit_value` |
| 7 | INR only; no currency carried on amounts | `settings.CURRENCY` |
| — | Every final approval routes to the CEO, who may also be Administrator | `capabilities.FINAL_APPROVALS` |
| — | Approval threshold and vendor-minimum are settings, not constants | `settings.APPROVAL_THRESHOLD` |
| — | Server-rendered templates, not React, to keep the stack decision open | `apps/ui/` |
| — | Append-only via DB trigger rather than revoked grants (superusers bypass grants) | `platform_core/migrations/0002` |
| — | Removal is a property of a change, not a sixth reconciliation outcome | `engineering/reconciliation.py` |
| — | "Needs approval" is a returned state, not a raised exception — raising rolled back the state change that reported it | `procurement/services.submit_purchase_order` |
| — | The lock protects commercial terms, not lifecycle status — otherwise it blocks the process it exists to protect | `Approvable.post_lock_writable` |
| — | One-command start: bootstrap is idempotent and loads the demo only into an empty database, so a restart never eats entered data | `platform_core/management/commands/bootstrap.py` |
| — | The demo's ad-hoc roles are namespaced `demo_*` — sharing a role code with `seed_roles` meant `get_or_create` silently returned the seeded role and ignored the demo's capability list | `core/management/commands/demo.py` |

## Open questions

Decided and built:

- **Stack** stays as it is — PostgreSQL + Django, server-rendered.
- **Currency: INR only.** No currency is carried on any amount. When
  multi-currency is needed it costs a currency column on `cost_entry`, a rate
  at posting, and a reporting decision about which rate to report at. Every
  existing row is INR, so back-filling is trivial — the cost is in the
  reporting rules, not the migration. Deliberately not pre-built.
- **All final approvals route to the CEO**, who may also hold Administrator.
  See `capabilities.FINAL_APPROVALS`.

Still open, none of them blocking:

- **Which item categories carry a wastage tolerance, and at what %.** Every
  category is at 0 today, so the ceiling is the exact BOQ quantity.
- **A value below which the three-vendor rule does not apply.**
  `RFQ_MINIMUM_VENDORS_BELOW_VALUE` is 0, so three quotes are required at every
  value. This is the rule buyers will meet most often.
- **A cap on the PM emergency ceiling override.** None set.
- **Director sign-off on large BOQs** cannot key off value — Discern's BOQ
  documents carry no rates. Needs a different trigger (line count, project
  value) or dropping.
- **Warehouse vs location per site** — built as a location hierarchy. Confirm.
- **Section sign-off level** — built as the preparing Manager's own
  confirmation. Confirm.
- **Integration boundary** — statutory accounting, GST, banking.
- **BOQ approval threshold:** Discern's BOQ documents carry no rates, so a
  value-based approval rule on release can never fire. Needs a different trigger
  if Director sign-off is wanted on large BOQs.

## On real data

Discern does not yet have data that maps cleanly onto these models, and that is
expected. The schema is the deliverable at this stage: it gives a target to map
actual records onto later, either by import or by entry through the app. Two
consequences that have already shaped the design:

- `BoqLine.item` is **nullable** and `description` carries the text, because the
  real BOQ files have no item-master reference and no rate.
- The importer (`engineering/importers.py`) reads Discern's actual spreadsheet
  layout — banner rows, header row, then SL/DESCRIPTION/UNIT/QTY — so historical
  BOQs can be loaded as an opening revision.

Where a real document does not fit, the model changes, not the document.

## Working notes

- Container is ephemeral. Every phase ends with a `git am` patch sent to the
  user; nothing depends on the container surviving.
- Postgres in this environment sometimes stops between sessions. Restart:
  `su postgres -s /bin/bash -c '/usr/lib/postgresql/16/bin/pg_ctl -D /tmp/pgdata -l /tmp/pg.log -o "-p 5432 -k /tmp/pgsock -c listen_addresses=127.0.0.1" start'`
- Run tests with `POSTGRES_HOST=127.0.0.1 POSTGRES_USER=postgres POSTGRES_DB=discern python3 -m pytest`.
