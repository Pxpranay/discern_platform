# Data Model

Companion to [`01-process-design.md`](01-process-design.md) and [`02-architecture.md`](02-architecture.md). Entities grouped by owning module. Ledger tables are specified in the architecture document (§4) and referenced here.

---

## Identity & Access

| Entity | Key fields |
|---|---|
| `app_user` | name, email, phone, active, is_administrator |
| `role` | code, name, capabilities (jsonb: `["purchase_order:approve", …]`) |
| `user_role` | user, role — composable; a user may hold several |
| `project_assignment` | user, project, role — scopes every query the user makes |
| `audit_entry` | actor, entity_type, entity_id, action, before (jsonb), after (jsonb), reason, at |
| `admin_override` | actor, entity_type, entity_id, before (jsonb), after (jsonb), **reason (required)**, at |

`admin_override` is separate from `audit_entry` deliberately — post-approval overrides are the Directors' review queue and must be trivially listable, not filtered out of a general log.

---

## CRM

| Entity | Key fields |
|---|---|
| `lead` | source, client_name, contact, site_location, work_type, assigned_to, stage, estimated_value |
| `opportunity` | lead, stage (`new` → `site_visit` → `estimating` → `quoted` → `won`/`lost`), rough_order_value, expected_close, won_at, lost_reason |
| `activity` | opportunity, type, due_date, assigned_to, completed_at, notes |
| `site_visit` | opportunity, visited_at, by, findings, photographs, rough_estimate |

---

## Sales

| Entity | Key fields |
|---|---|
| `client` | name, gstin, billing_address, contacts, payment_terms |
| `quotation` | opportunity, client, number, revision, status, valid_until, total_value |
| `quotation_lot` | quotation, name, kind (`itemized` \| `lump_sum_sitc`), price, scope_description |
| `quotation_line` | quotation_lot, item?, description, qty, uom, rate — absent for a lump-sum lot |
| `order` | quotation, client, number, confirmed_at, **committed_delivery_date**, kickoff_approved_by, kickoff_approved_at, status |
| `lot` | order, name, kind, **price**, scope_description, project? |
| `change_order` | order, lot?, number, price_delta, new_committed_date?, reason, approved_by, approved_at |
| `client_invoice` | order, lot, number, **invoice_date**, amount, status |

`lot` is the pivot of the whole model (process §3.2). It is the only thing that carries an agreed price, and every BOQ line, commitment and cost entry traces to one. `lot.kind` distinguishes itemized from lump-sum SITC; the difference affects only whether `quotation_line` rows exist, never how anything downstream behaves.

`committed_delivery_date` is the schedule ceiling. A `change_order` is the only way it moves.

---

## Projects

| Entity | Key fields |
|---|---|
| `project` | order, code, name, client, site_address, budget, status, project_manager, date_start, date_end, **effective_committed_date** |
| `schedule_phase` | project, name, kind (`site_visit` \| `boq_prep` \| `procurement` \| `construction`), sequence, planned_start, planned_end, actual_start, actual_end, is_complete |
| `schedule_extension` | project, previous_committed_date, new_committed_date, **client_agreement_reference (required)**, authorized_by, at |

`effective_committed_date` starts equal to `order.committed_delivery_date` and moves only via `schedule_extension`. No `schedule_phase.planned_end` may exceed it — the ceiling of process §4.3.

**Correction to an earlier draft of this document:** that rule was described here as a database CHECK constraint. It cannot be one — the comparison spans two tables (`schedule_phase` and `project`), and a row-level CHECK can only see its own row. It is enforced in the domain service instead (`apps/projects/services.py`), which every write path goes through. A per-row CHECK does hold the intra-row rule that a phase cannot end before it starts.

Procurement is `kind = 'procurement'` with a `sequence`, so a project needing three staged procurement windows simply has three rows. No special-casing.

---

## Engineering (BOQ)

| Entity | Key fields |
|---|---|
| `boq_revision` | project, revision_number, status (`draft` → `sections_signed` → `released` \| `sent_back`), prepared_at, released_by, released_at, **locked_at**, total_value |
| `boq_section` | boq_revision, discipline (`goods` \| `service`), owner, signed_off_by, signed_off_at, **is_not_applicable** |
| `boq_line` | boq_section, **lot** (required), item?, description, quantity, uom, estimated_rate, **route** (`SUPPLY` \| `FABRICATE` \| `SERVICE`), drawing_reference, notes |
| `boq_line_link` | boq_line, previous_revision_line — carries identity across revisions so the diff can match lines |

**One revision, two sections** (process §3.4). `is_not_applicable` on a section is what prevents the deadlock the earlier two-document design had: a materials-only project marks the service section not-applicable and releases normally.

`route` is fixed here and never re-decided downstream. `goods` sections carry `SUPPLY`/`FABRICATE`; `service` sections carry `SERVICE` — enforced by a check constraint on `(discipline, route)`.

`boq_line_link` exists because the reconciliation diff needs line identity across revisions. Without it, a re-typed description reads as "old line deleted, new line added" and produces a spurious return plus a spurious purchase.

**Derived, never stored:**
- `committed_qty` = `SUM(commitment_entry.qty_delta)` for the line
- `received_qty` = net inbound `stock_move` for the line
- `headroom` = `quantity + tolerance − committed_qty`

Principle 1 of the design (process §2): no process ever increments a stored total.

---

## Procurement

| Entity | Key fields |
|---|---|
| `vendor` | name, gstin, address, contacts, trades (jsonb), is_empanelled, payment_terms, rating |
| `vendor_rate` | vendor, item, rate, valid_from, valid_until — the agreed rates that let service orders skip RFQ |
| `procurement_request` | project, **source** (`BOQ_RELEASE` \| `SITE_REQUISITION` \| `FABRICATION_SHORTFALL`), boq_revision?, requested_by, approved_by?, approved_at?, status, **is_site_raised** |
| `procurement_request_line` | procurement_request, boq_line?, item, description, quantity, uom, required_by, parent_fabrication_order? |
| `rfq` | procurement_request, number, issued_at, closes_at, status, **min_vendors_waived_reason** |
| `rfq_vendor` | rfq, vendor, sent_at, responded_at, status |
| `rfq_quote_line` | rfq_vendor, procurement_request_line, quoted_rate, quoted_qty, delivery_date, terms, notes |
| `award` | rfq, procurement_request_line, **winning_vendor**, awarded_by, awarded_at, comparison_snapshot (jsonb), notes |
| `purchase_order` | vendor, project, number, status, total_value, approved_by?, approved_at?, **locked_at**, expected_delivery |
| `purchase_order_line` | purchase_order, **lot**, boq_line?, item, description, quantity, uom, rate, received_qty (derived) |
| `po_amendment` | purchase_order_line, previous_qty, new_qty, reason, actor, at |

**One request record, three sources** (process §4.6) — the `source` field is the only difference, and downstream logic is written once.

`rfq.min_vendors_waived_reason` is how the ≥3-vendor rule stays enforceable without becoming a deadlock: a genuinely single-source item proceeds with a recorded reason, and the blank case is blocked.

`award.comparison_snapshot` freezes what the Purchase Manager actually saw at the moment of award. This is the audit trail that makes a fully discretionary award defensible without demanding a written justification — the record shows the alternatives and the choice, which is stronger than a free-text reason.

---

## Fabrication

| Entity | Key fields |
|---|---|
| `bill_of_materials` | item, revision, is_active, notes |
| `bom_component` | bill_of_materials, item, quantity, uom, wastage_pct |
| `fabrication_order` | project, **lot**, boq_line, item, quantity, bill_of_materials, status, **mode** (`in_house` \| `job_work`), vendor?, started_at, completed_at, **locked_at** |
| `fabrication_step` | fabrication_order, sequence, name, status, completed_by, completed_at |
| `material_consumption` | fabrication_order, item, planned_qty, actual_qty, stock_move |

`mode` covers decision #3 of the blocking list. `job_work` issues components out to a vendor's location and receives the finished item back — two extra stock moves and a subcontract cost entry, on the same order. `in_house` consumes at Discern's own works location.

`material_consumption` records planned against actual, so a run consuming materially more than its BOM is **visible** rather than absorbed silently. Process §4.8 deliberately exempts raw materials from the BOQ ceiling; this is the compensating control.

---

## Subcontracts

| Entity | Key fields |
|---|---|
| `service_order` | project, **lot**, boq_line, vendor, number, scope_description, quantity, uom, rate, total_value, status, approved_by?, **locked_at** |
| `service_progress` | service_order, reported_at, reported_by, percent_complete, quantity_done, notes, photographs |
| `service_certification` | service_order, certified_by, certified_at, certified_quantity, certified_value, is_final, **running_bill_number** |
| `vendor_bill` | vendor, project, lot, source_type (`purchase_order` \| `service_certification` \| `job_work`), source_id, number, bill_date, amount, status |

`service_progress` is logged by anyone with visibility — site coordinators included. `service_certification` is the gate that releases billing, and it is a distinct act by a distinct role. Separating "someone reported 60%" from "someone certified 60% as billable" is the whole point.

`running_bill_number` supports progressive subcontractor billing, which is the norm rather than an edge case.

---

## Inventory

| Entity | Key fields |
|---|---|
| `item` | code, name, category, uom, item_type (`goods` \| `service`), default_route, is_stocked, hsn_code |
| `location` | code, name, kind (`site` \| `yard` \| `works` \| `vendor` \| `transit`), project?, parent? |
| `expected_receipt` | purchase_order_line, location, expected_qty, expected_date, status |
| `goods_receipt` | expected_receipt?, purchase_order_line, location, received_qty, received_by, received_at, status |
| `receipt_verification` | goods_receipt, **verified_by (Site Engineer)**, verified_at, accepted_qty, rejected_qty, discrepancy_notes, photographs |
| `stock_transfer` | from_location, to_location, reason, requested_by, approved_by, status, excess_flag? |
| `excess_stock_flag` | goods_receipt, item, quantity, location, project, flagged_by, flagged_at, reason (`dead` \| `available_for_other_project`), resolution, resolved_at |
| `material_return` | goods_receipt, purchase_order_line, quantity, reason, debit_note_number, status |

`location.project` is what scopes stock to a project (process §5.4). `location.kind = 'vendor'` supports job work; `transit` supports yard-to-site movement.

**`receipt_verification` is a separate entity from `goods_receipt` on purpose.** The Store Keeper records what arrived; the Site Engineer verifies it. Cost posts on verification, not on receipt. Collapsing the two into flags on one row is how "verification" quietly becomes a checkbox the receiving user ticks themselves.

---

## Finance

| Entity | Key fields |
|---|---|
| `site_expense` | project, category (`room_rent` \| `water` \| `conveyance` \| `fooding` \| `miscellaneous`), amount, expense_date, incurred_by, submitted_by, approved_by?, status, receipt_attachment |
| `cost_entry` | See architecture §4.2 — the profitability authority |

Site expenses are ordinary cost entries in the same ledger as material and subcontract cost. This is the structural fix for the gap the earlier design documented in its §6.14: an entire category of real spend that could not reach the profitability figure and needed a separate report to see at all.

---

## Platform

| Entity | Key fields |
|---|---|
| `commitment_entry` | Architecture §4.1 — the quantity ceiling |
| `stock_move` | Architecture §4.3 — the stock authority |
| `approval_rule` | document_type, condition, threshold, required_role, sequence |
| `approval_request` | document_type, document_id, rule, status, requested_at |
| `approval_action` | approval_request, actor, action (`approve` \| `reject` \| `send_back`), comments, at |
| `outbox_event` | event_name, payload (jsonb), created_at, processed_at?, attempts, last_error, status |
| `notification` | user, event, title, body, entity_type, entity_id, read_at |

---

## Entity Relationship Summary

```
Lead → Opportunity → Quotation ─┬─▶ QuotationLot
                                │
                                ▼
                              Order ──▶ Lot ◀────────────────────┐
                                │         │                      │
                                ▼         │  (every downstream    │
                            Project ◀─────┘   record carries      │
                             │  │             its lot)            │
                             │  └──▶ SchedulePhase                │
                             ▼                                    │
                        BoqRevision ──▶ BoqSection ──▶ BoqLine ───┤
                                                          │       │
                    ┌─────────────────────────────────────┤       │
                    │                 │                   │       │
                    ▼                 ▼                   ▼       │
          ProcurementRequest   FabricationOrder     ServiceOrder   │
                    │                 │                   │       │
                    ▼                 ▼                   ▼       │
                  RFQ            FabStep/Consump   ServiceProgress │
                    │                 │                   │       │
                    ▼                 │                   ▼       │
                 Award                │            Certification  │
                    │                 │                   │       │
                    ▼                 │                   │       │
             PurchaseOrder            │                   │       │
                    │                 │                   │       │
                    ▼                 │                   │       │
             GoodsReceipt             │                   │       │
                    │                 │                   │       │
                    ▼                 │                   │       │
          ReceiptVerification         │                   │       │
                    │                 │                   │       │
                    └────────┬────────┴─────────┬─────────┘       │
                             ▼                  ▼                 │
                     ┌───────────────┐   ┌──────────────┐         │
                     │ StockMove     │   │ VendorBill   │         │
                     └───────────────┘   └──────────────┘         │
                             │                  │                 │
        SiteExpense ─────────┼──────────────────┤                 │
        ClientInvoice ───────┼──────────────────┤                 │
                             ▼                  ▼                 │
                    ╔════════════════════════════════╗            │
                    ║   CostEntry  (project + lot)   ║────────────┘
                    ╚════════════════════════════════╝
                                    │
                                    ▼
                    Profitability, per project and per lot

        Every quantity-authorizing document above also writes:
                    ╔════════════════════════════════╗
                    ║  CommitmentEntry (per BoqLine) ║
                    ╚════════════════════════════════╝
```

Two things to read from this diagram. **Everything converges on the cost ledger** — there is no path by which real spend reaches a project without appearing in profitability. And **`Lot` threads the entire model** from the price the client agreed to the last rupee of cost incurred against it, which is what makes per-lot margin a query rather than a project.
