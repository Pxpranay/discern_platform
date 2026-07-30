# Progress Log

**Purpose:** so a new session can pick up cheaply without re-reading the whole
codebase or the whole conversation. Read this file first; it is kept current
with every commit.

---

## State

| | |
|---|---|
| **Built** | Phases 0, 1, 2, 3 + web app + admin/RBAC |
| **Tests** | 192 passing against real PostgreSQL, order-independent |
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

## Next

**Phase 4 — Fabrication and subcontracts.** Bills of materials and fabrication
orders (in-house and job-work); raw-material shortfall raising child
procurement requests; service orders direct to empanelled subcontractors with
progress logging and running-bill certification; site expenses; dead/excess
stock flagging with inter-project transfer and paired cost entries.

Then Phase 5: dashboards and the mobile site screens.

Blocking decisions that Phase 4 needs answered: **#3** (in-house vs job-work
fabrication) and **#4** (transfer valuation basis). I will build both to the
recommended defaults unless told otherwise, as with #1 and #2.

## Decisions taken

| # | Decision | Where |
|---|---|---|
| 1 | Wastage tolerance per item category, default 0% | `core.ItemCategory.wastage_tolerance_pct` |
| 2 | PM emergency ceiling override, logged | `ceiling.reserve_headroom(override_actor=…)` |
| — | Server-rendered templates, not React, to keep the stack decision open | `apps/ui/` |
| — | Append-only via DB trigger rather than revoked grants (superusers bypass grants) | `platform_core/migrations/0002` |
| — | Removal is a property of a change, not a sixth reconciliation outcome | `engineering/reconciliation.py` |
| — | "Needs approval" is a returned state, not a raised exception — raising rolled back the state change that reported it | `procurement/services.submit_purchase_order` |

## Open questions

- **Stack:** Supabase-native rewrite vs Supabase-as-Postgres keeping the Python
  core. Deferred until the app's shape was concrete — it now is.
- **Blocking decisions 3–8** in `05-decisions.md`: job-work fabrication,
  transfer valuation basis, warehouse vs location per site, section sign-off
  level, multi-currency, integration boundary. Needed before Phase 3–4.
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
