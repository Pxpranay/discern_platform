# System Architecture

Companion to [`01-process-design.md`](01-process-design.md). This document covers how the platform is built, not what it does.

---

## 1. Shape: Modular Monolith

**One deployable application, eleven modules with enforced boundaries.**

Not microservices. This is a deliberate choice, and the reason is transactional integrity: the commitment ledger, the cost ledger and the documents that write to them **must** commit atomically. When a purchase order is confirmed, the order row and its commitment entry either both exist or neither does. Across a service boundary that becomes a distributed transaction — a saga, a compensating-action framework, an eventual-consistency window during which the quantity ceiling is not actually enforced.

The ceiling is the single strongest requirement in the whole design. Architecting in a way that makes it merely eventually true would be trading the requirement for a deployment diagram.

**Boundaries are still real.** Each module owns its tables and exposes a service interface; cross-module reads go through that interface, never through another module's tables directly. An import-linter rule fails the build on a violation. If a module ever genuinely needs to scale independently, the seam is already there.

---

## 2. Stack

| Layer | Choice | Reasoning |
|---|---|---|
| **Database** | PostgreSQL 16 | The design rests on append-only ledgers with heavy aggregate queries, row-level locking (`SELECT FOR UPDATE` for the ceiling), and strict referential integrity. Partial and covering indexes, window functions, `numeric` for money, and materialized views for portfolio roll-ups |
| **Backend** | Python 3.12 · Django 5 · Django REST Framework | ERP-shaped work: dozens of entities, constant schema evolution, role-based access, audit. Django brings migrations, ORM, auth and an admin surface for back-office edge cases. Python also matches the team's existing skills from the Odoo work, so the transition is a framework change, not a language change |
| **Async** | Celery · Redis | Event handlers, notification fan-out, scheduled reconciliation, report pre-computation |
| **Frontend** | React 18 · TypeScript · Vite · TanStack Query | Data-dense screens with live derived numbers. TypeScript matters here because the domain has many similar-shaped entities that are catastrophic to confuse |
| **Site screens** | Same SPA, mobile-first routes | Receipt, verification, progress, expenses and stock flagging get purpose-built phone layouts, offline-tolerant for weak site connectivity |
| **Reporting** | SQL views + a PDF renderer | Comparison statements, BOQ prints and running bills are documents, not screens |
| **Auth** | Session-based, optional SSO later | Internal users only. No public surface |

**On Django specifically:** the alternative worth naming is FastAPI + SQLAlchemy, which gives a cleaner domain layer and better async. It also means building migrations tooling, an admin surface, and an auth/permissions layer that Django ships. For a team of this size on a schedule this long, Django's batteries are worth more than FastAPI's purity. The domain logic sits in plain Python service modules either way, so this choice is reversible at the edges and not at the core.

---

## 3. Layering

```
HTTP / REST  ──▶  Application services  ──▶  Domain  ──▶  Repositories  ──▶  PostgreSQL
                        │                       │
                        ▼                       ▼
                  Event outbox            Invariants
                        │                (ceiling, locking,
                        ▼                 state machines)
                  Celery workers
```

**The rule that matters:** business invariants live in the domain layer, never in a serializer, a view, or a database trigger. There is exactly one code path that can authorize quantity against a BOQ line, and it is a domain function. Anything reaching the database — REST, admin, management command, background job, data fix — goes through it.

A validation implemented in a serializer is a validation the admin screen does not have.

---

## 4. The Three Ledgers

All three share one shape: **append-only, immutable rows, derived totals, no updates, no deletes.** Enforced by revoking `UPDATE` and `DELETE` at the database role level, not merely by convention.

### 4.1 Commitment Ledger

```sql
CREATE TABLE commitment_entry (
  id            bigserial PRIMARY KEY,
  boq_line_id   bigint    NOT NULL REFERENCES boq_line(id),
  project_id    bigint    NOT NULL REFERENCES project(id),
  document_type text      NOT NULL,   -- purchase_order | service_order | fabrication_order | site_requisition
  document_id   bigint    NOT NULL,
  qty_delta     numeric(18,4) NOT NULL,  -- signed: + authorizes, - releases
  reason        text      NOT NULL,
  actor_id      bigint    NOT NULL REFERENCES app_user(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON commitment_entry (boq_line_id);
```

The one function every route calls:

```python
def reserve_headroom(boq_line_id, qty, document, actor, reason):
    """Consume ceiling headroom, or raise CeilingExceeded. Caller's transaction."""
    line = BoqLine.objects.select_for_update().get(pk=boq_line_id)   # serializes concurrent callers
    committed = commitment_total(boq_line_id)                        # SUM(qty_delta)
    headroom  = line.quantity + tolerance_for(line) - committed
    if qty > headroom:
        raise CeilingExceeded(line, requested=qty, available=headroom)
    CommitmentEntry.objects.create(
        boq_line_id=boq_line_id, project_id=line.project_id,
        document_type=document.type, document_id=document.id,
        qty_delta=qty, actor=actor, reason=reason,
    )
```

`release_headroom` is the same call with a negative delta, used by cancellation, amendment and return. Because release is consumption with the opposite sign, **returns net out arithmetically** — the defect in the earlier design's approach (§9.2 of the process document), where a returned line's headroom was lost permanently.

The `select_for_update` is what closes the race. Two buyers confirming simultaneously serialize on the BOQ line row; the second sees the first's commitment before deciding.

### 4.2 Cost Ledger

```sql
CREATE TABLE cost_entry (
  id             bigserial PRIMARY KEY,
  project_id     bigint      NOT NULL REFERENCES project(id),
  lot_id         bigint      REFERENCES lot(id),
  boq_line_id    bigint      REFERENCES boq_line(id),
  category       text        NOT NULL,   -- MATERIAL | FABRICATION | SUBCONTRACT
                                         -- SITE_EXPENSE | STOCK_IN | STOCK_OUT | REVENUE
  amount         numeric(18,2) NOT NULL, -- signed
  source_type    text        NOT NULL,
  source_id      bigint      NOT NULL,
  effective_date date        NOT NULL,   -- when it economically occurred
  posted_at      timestamptz NOT NULL DEFAULT now(),  -- when we recorded it
  reverses_id    bigint      REFERENCES cost_entry(id),
  actor_id       bigint      NOT NULL REFERENCES app_user(id)
);
CREATE INDEX ON cost_entry (project_id, category, effective_date);
CREATE INDEX ON cost_entry (lot_id) WHERE lot_id IS NOT NULL;
```

`effective_date` and `posted_at` are separate on purpose: a bill entered late belongs to the period it occurred in, but the audit trail must still show when it was actually recorded. Conflating the two is how a closed period silently changes.

Corrections post a row with `reverses_id` set. Nothing is edited.

### 4.3 Stock Ledger

```sql
CREATE TABLE stock_move (
  id               bigserial PRIMARY KEY,
  item_id          bigint    NOT NULL REFERENCES item(id),
  from_location_id bigint    REFERENCES location(id),  -- NULL = external (receipt)
  to_location_id   bigint    REFERENCES location(id),  -- NULL = external (return/issue)
  quantity         numeric(18,4) NOT NULL CHECK (quantity > 0),
  unit_value       numeric(18,4),
  source_type      text      NOT NULL,
  source_id        bigint    NOT NULL,
  effective_at     timestamptz NOT NULL,
  actor_id         bigint    NOT NULL REFERENCES app_user(id)
);
```

On-hand is a sum of inbound minus outbound per `(item, location)`, maintained as a materialized view refreshed on write for the availability screens, with the ledger remaining the authority. Cross-location availability — the earlier design's "genuinely custom joined report" — is one query here.

---

## 5. Approval Engine

Declarative rules, one enforcement path:

```python
ApprovalRule(
    document_type="purchase_order",
    condition="total_value > threshold",
    threshold=Decimal("500000"),
    required_role="purchase_manager",
    sequence=1,
)
```

A document declares its states and which transition requires approval. The engine:

1. Resolves applicable rules on transition.
2. Creates approval requests and notifies the holders of those roles.
3. On the final approval, advances the state and **sets `locked_at`**.
4. Records every action immutably.

**Locking is enforced in the domain base class**, so it holds for every write path:

```python
class Approvable(models.Model):
    locked_at = models.DateTimeField(null=True)

    def save(self, *args, override: AdminOverride | None = None, **kwargs):
        if self.locked_at and not override:
            raise RecordLocked(self)
        if override:
            override.record(self, before=self.snapshot())  # immutable audit
        super().save(*args, **kwargs)
```

An Administrator override is a first-class object with a mandatory reason, not a bypassed check. Overrides appear on the Directors' dashboard (§6.4 of the process document) — permitted, but never quiet.

---

## 6. Event Bus

**Transactional outbox.** Events are written in the same transaction as the state change, so a hand-off cannot be lost to a crash between the two:

```python
with transaction.atomic():
    order.confirm()
    reserve_headroom(line.boq_line_id, line.qty, order, actor, "PO confirmed")
    emit("PurchaseOrderConfirmed", {"order_id": order.id})   # → outbox row
```

A Celery worker drains the outbox: at-least-once delivery, handler-level idempotency keys, exponential backoff, and a **dead-letter queue** an Administrator can inspect and replay. The earlier design's 19 automation rules had no answer for a hand-off that fails halfway; at this many hand-offs, some will.

Handlers are ordinary functions registered against event names, unit-testable without HTTP or a broker.

---

## 7. Access Control

Three layers:

1. **Role → capability.** A role grants verbs on document types (`purchase_order:approve`).
2. **Project scoping.** A user's project assignments filter every query. Enforced by a base queryset manager, not by remembering to filter in each view.
3. **Field-level.** A handful of fields (vendor pricing, margin) are role-gated.

Roles compose — one person may hold Purchase Manager and cover Construction duties on a smaller project. Every assignment change is audited.

---

## 8. Data Integrity

| Concern | Mechanism |
|---|---|
| Ledger immutability | `UPDATE`/`DELETE` revoked for the application role; append-only by grant, not convention |
| Ceiling races | `SELECT FOR UPDATE` on the BOQ line inside the authorizing transaction |
| Money | `numeric`, never float. Rounding at presentation only |
| Quantities | `numeric(18,4)` — construction quantities are fractional |
| Schedule constraint | Check constraint: no phase date beyond the project's effective committed date |
| Orphan cost | `cost_entry.project_id` is `NOT NULL`. Untagged cost is impossible by schema |
| Audit | Every mutation records actor, timestamp and before/after on approval-carrying models |

---

## 9. Environments & Operations

- **Local** — Docker Compose: Postgres, Redis, backend, frontend, worker.
- **Staging** — production-shaped, restored from anonymized production data.
- **Production** — single application server plus worker to start; managed Postgres with PITR. Vertical scaling carries this workload comfortably at Discern's scale.

**Backups:** managed Postgres point-in-time recovery, with restores rehearsed rather than assumed.

**Observability:** structured logs; error tracking; dashboards for outbox lag, dead-letter depth, and ceiling-block frequency. That last one is a **process** signal — a spike means BOQs are being under-specified, which is worth knowing early.

**Testing:** the invariants get property-based tests. Specifically — a randomized sequence of orders, amendments, cancellations and returns against a BOQ line must **never** leave committed quantity above the line's quantity, and must always return headroom to exactly the right figure when everything is cancelled. That single test class protects the design's central promise, and it is the test to write first.
