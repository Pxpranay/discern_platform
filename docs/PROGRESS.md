# Progress Log

**Purpose:** so a new session can pick up cheaply without re-reading the whole
codebase or the whole conversation. Read this file first; it is kept current
with every commit.

---

## State

| | |
|---|---|
| **Built** | Phases 0–4 + web app + admin/RBAC |
| **Tests** | 235 passing against real PostgreSQL, order-independent |
| **Stack** | PostgreSQL 16 · Django 5 · server-rendered templates. Supabase move still open |
| **Login** | `demo` / `discern2026` (`make seed` then `make run`) |
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

## Next

**Phase 5 — Dashboards and mobile site screens.** The Project Manager's single
dashboard (BOQ status, site progress, purchase movement, schedule,
profitability), the Construction Manager's expense-vs-income sheet, the
Purchase Manager's cross-warehouse stock and value view, the Directors'
portfolio roll-up with the override log, drill-through everywhere, and
mobile-first screens for the site roles — receipt, verification, progress,
expenses and stock flagging.

Build plan §7 is blunt about the risk: if receipt and verification are painful
on a phone at a site gate, the ledgers stay empty and every dashboard lies.

## Decisions taken

| # | Decision | Where |
|---|---|---|
| 1 | Wastage tolerance per item category, default 0% | `core.ItemCategory.wastage_tolerance_pct` |
| 2 | PM emergency ceiling override, logged | `ceiling.reserve_headroom(override_actor=…)` |
| 3 | Both fabrication modes — in-house and job work | `fabrication.FabricationOrder.mode` |
| 4 | Transfer valued at original purchase cost | `stock.valuation_at`, `ExcessStockFlag.unit_value` |
| — | Server-rendered templates, not React, to keep the stack decision open | `apps/ui/` |
| — | Append-only via DB trigger rather than revoked grants (superusers bypass grants) | `platform_core/migrations/0002` |
| — | Removal is a property of a change, not a sixth reconciliation outcome | `engineering/reconciliation.py` |
| — | "Needs approval" is a returned state, not a raised exception — raising rolled back the state change that reported it | `procurement/services.submit_purchase_order` |
| — | The lock protects commercial terms, not lifecycle status — otherwise it blocks the process it exists to protect | `Approvable.post_lock_writable` |

## Open questions

- **Stack:** Supabase-native rewrite vs Supabase-as-Postgres keeping the Python
  core. Deferred until the app's shape was concrete — it now is.
- **Blocking decisions 5–8** in `05-decisions.md`: warehouse vs location per
  site, section sign-off level, multi-currency, integration boundary. #3 and #4
  are now built to their recommended defaults and are configuration if Discern
  decides otherwise.
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
