"""Dashboard queries.

Every figure here is a query over the ledgers. Nothing is stored, cached or
pre-aggregated, which is why no two screens can disagree — the Construction
Manager's expense sheet and the Project Manager's are the same query with a
different viewer.

Each figure also carries the ids needed to drill through to the documents
behind it: a number nobody can trace is a number nobody trusts.
"""

from datetime import date
from decimal import Decimal

from django.db.models import Count, Q, Sum

from apps.core.models import Item, Location, Project
from apps.engineering.models import BoqRevision, ReconciliationOutcome
from apps.fabrication.models import FabricationOrder
from apps.finance.models import SiteExpense
from apps.inventory.models import ExcessStockFlag, GoodsReceipt, StockTransfer
from apps.platform_core.models import AdminOverride, CostEntry, OutboxEvent, StockMove
from apps.platform_core.services import costing
from apps.procurement.models import ProcurementRequest, PurchaseOrder, PurchaseOrderLine, Rfq
from apps.subcontracts.models import ServiceOrder

ZERO = Decimal("0")


# ------------------------------------------------------ Project Manager
def boq_status(project) -> dict:
    """Current revision, its state, and what reconciliation left outstanding."""
    latest = project.boq_revisions.order_by("-revision_number").first()
    released = project.boq_revisions.filter(status=BoqRevision.RELEASED).order_by(
        "-revision_number"
    ).first()
    outstanding = ReconciliationOutcome.objects.filter(
        revision__project=project
    ).exclude(action=ReconciliationOutcome.NONE).count()

    return {
        "latest": latest,
        "released": released,
        "revision_number": released.revision_number if released else None,
        "is_draft_open": bool(latest and not latest.is_released),
        "line_count": (
            released.sections.aggregate(n=Count("lines"))["n"] if released else 0
        ),
        "outstanding_actions": outstanding,
    }


def site_progress(project) -> dict:
    """Rolled up across every service order — what the site team reported,
    and what has actually been certified as billable."""
    orders = list(project.service_orders.all())
    if not orders:
        return {"orders": 0, "percent": None, "certified_value": ZERO, "outstanding_value": ZERO}

    total_qty = sum((o.quantity for o in orders), ZERO)
    certified = sum((o.certified_qty for o in orders), ZERO)
    return {
        "orders": len(orders),
        "percent": (certified / total_qty * 100) if total_qty else ZERO,
        "certified_value": sum((o.certified_qty * o.rate for o in orders), ZERO),
        "outstanding_value": sum((o.outstanding_qty * o.rate for o in orders), ZERO),
        "complete": sum(1 for o in orders if o.status == ServiceOrder.COMPLETE),
    }


def purchase_movement(project) -> dict:
    """Ordered, received and billed — the three numbers that differ, and the
    reason a single 'spend' figure misleads."""
    lines = PurchaseOrderLine.objects.filter(
        purchase_order__project=project,
        purchase_order__status=PurchaseOrder.CONFIRMED,
    ).select_related("purchase_order")

    ordered = sum((line.amount for line in lines), ZERO)
    received = sum((line.received_qty * line.rate for line in lines), ZERO)
    billed = costing.project_total(project.pk, CostEntry.MATERIAL)

    return {
        "orders": PurchaseOrder.objects.filter(project=project).count(),
        "awaiting_approval": PurchaseOrder.objects.filter(
            project=project, status=PurchaseOrder.AWAITING_APPROVAL
        ).count(),
        "ordered": ordered,
        "received": received,
        "outstanding": ordered - received,
        "billed": billed,
        "requests_open": ProcurementRequest.objects.filter(
            project=project, status=ProcurementRequest.AWAITING_APPROVAL
        ).count(),
    }


def schedule_status(project) -> dict:
    phases = list(project.schedule_phases.all())
    today = date.today()
    committed = project.effective_committed_date
    latest_end = max((p.planned_end for p in phases), default=None)
    return {
        "phases": len(phases),
        "complete": sum(1 for p in phases if p.is_complete),
        "overdue": [p for p in phases if not p.is_complete and p.planned_end < today],
        "committed_date": committed,
        "latest_planned_end": latest_end,
        "headroom_days": (committed - latest_end).days if committed and latest_end else None,
        "extensions": project.schedule_extensions.count(),
    }


def project_dashboard(project) -> dict:
    """Everything the Project Manager is accountable for, in one place.

    Profitability is the PM's named responsibility, so the design deliberately
    refuses to scatter what that number depends on across separate screens.
    """
    profit = costing.profitability(project.pk)
    return {
        "project": project,
        "boq": boq_status(project),
        "progress": site_progress(project),
        "purchase": purchase_movement(project),
        "schedule": schedule_status(project),
        "profit": profit,
        "lots": [(lot, costing.lot_profitability(lot.pk)) for lot in project.lots.all()],
        "excess_flags": ExcessStockFlag.objects.filter(
            project=project, status=ExcessStockFlag.OPEN
        ),
        "fabrication": FabricationOrder.objects.filter(project=project).count(),
    }


# ------------------------------------------------------ Purchase Manager
def warehouse_stock(item_id=None) -> list[dict]:
    """Stock and value across every location Discern operates.

    Keeping stock efficient across the whole company is the Purchase Manager's
    job, so this is not scoped to any one project.
    """
    rows = (
        StockMove.objects.values("item_id", "to_location_id")
        .annotate(qty=Sum("quantity"))
        .filter(to_location__isnull=False)
    )
    out = (
        StockMove.objects.values("item_id", "from_location_id")
        .annotate(qty=Sum("quantity"))
        .filter(from_location__isnull=False)
    )
    balance: dict[tuple[int, int], Decimal] = {}
    for row in rows:
        balance[(row["item_id"], row["to_location_id"])] = row["qty"]
    for row in out:
        key = (row["item_id"], row["from_location_id"])
        balance[key] = balance.get(key, ZERO) - row["qty"]

    items = {i.pk: i for i in Item.objects.all()}
    locations = {loc.pk: loc for loc in Location.objects.select_related("project")}

    from apps.platform_core.services.stock import valuation_at

    result = []
    for (item_pk, location_pk), qty in balance.items():
        if qty == ZERO or (item_id and item_pk != item_id):
            continue
        unit = valuation_at(item_pk, location_pk)
        result.append({
            "item": items.get(item_pk),
            "location": locations.get(location_pk),
            "project": locations.get(location_pk).project if locations.get(location_pk) else None,
            "on_hand": qty,
            "unit_value": unit,
            "value": (unit or ZERO) * qty,
        })
    return sorted(result, key=lambda r: (-(r["value"] or ZERO)))


def purchase_dashboard() -> dict:
    stock = warehouse_stock()
    return {
        "stock": stock,
        "total_value": sum((r["value"] for r in stock), ZERO),
        "locations": len({r["location"].pk for r in stock if r["location"]}),
        "excess": ExcessStockFlag.objects.filter(status=ExcessStockFlag.OPEN)
        .select_related("item", "project", "location"),
        "pending_transfers": StockTransfer.objects.filter(status=StockTransfer.PENDING),
        "rfqs_open": Rfq.objects.exclude(status__in=[Rfq.AWARDED, Rfq.CANCELLED]).count(),
        "orders_awaiting": PurchaseOrder.objects.filter(
            status=PurchaseOrder.AWAITING_APPROVAL
        ),
        "receipts_unverified": GoodsReceipt.objects.filter(status=GoodsReceipt.RECORDED).count(),
    }


# ---------------------------------------------------------- Directors
def portfolio() -> dict:
    """Every active project, and the governance queue.

    The override log sits here because an Administrator override is permitted
    but never quiet — the Directors see every one after the fact.
    """
    rows = []
    for project in Project.objects.filter(is_active=True).select_related("client"):
        profit = costing.profitability(project.pk)
        schedule = schedule_status(project)
        rows.append({
            "project": project,
            "revenue": profit["revenue"],
            "cost": profit["cost"],
            "margin": profit["margin"],
            "pct": (profit["margin"] / profit["revenue"] * 100) if profit["revenue"] else None,
            "overdue": len(schedule["overdue"]),
            "committed_date": schedule["committed_date"],
            "extensions": schedule["extensions"],
        })
    rows.sort(key=lambda r: r["margin"])

    revenue = sum((r["revenue"] for r in rows), ZERO)
    cost = sum((r["cost"] for r in rows), ZERO)
    return {
        "rows": rows,
        "revenue": revenue,
        "cost": cost,
        "margin": revenue - cost,
        "pct": (revenue - cost) / revenue * 100 if revenue else None,
        "overrides": AdminOverride.objects.select_related("actor")[:25],
        "dead_letters": OutboxEvent.objects.filter(status=OutboxEvent.DEAD),
        "at_risk": [r for r in rows if r["margin"] < ZERO or r["overdue"]],
    }


# ------------------------------------------------ Construction Manager
def expense_vs_income(project) -> dict:
    """Site running costs, subcontract bills and client invoices, dated.

    One query, two viewers — the Construction Manager sees this on their own
    screen and the Project Manager sees the same numbers on theirs.
    """
    entries = CostEntry.objects.filter(project=project).order_by("effective_date")
    expenses = entries.filter(category=CostEntry.SITE_EXPENSE)
    subcontract = entries.filter(category=CostEntry.SUBCONTRACT)
    revenue = entries.filter(category=CostEntry.REVENUE)

    pending = SiteExpense.objects.filter(project=project, status=SiteExpense.SUBMITTED)
    return {
        "project": project,
        "expenses": expenses,
        "expense_total": expenses.aggregate(t=Sum("amount"))["t"] or ZERO,
        "subcontract": subcontract,
        "subcontract_total": subcontract.aggregate(t=Sum("amount"))["t"] or ZERO,
        "revenue": revenue,
        "revenue_total": revenue.aggregate(t=Sum("amount"))["t"] or ZERO,
        "pending": pending,
        #: Shown separately rather than hidden: pending claims are real spend,
        #: and leaving them out flatters the margin exactly when it matters.
        "pending_total": pending.aggregate(t=Sum("amount"))["t"] or ZERO,
        "breakdown": expense_breakdown(project),
    }


def expense_breakdown(project) -> list[dict]:
    rows = (
        SiteExpense.objects.filter(project=project, status=SiteExpense.APPROVED)
        .values("category")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")
    )
    labels = dict(SiteExpense._meta.get_field("category").choices)
    return [
        {"category": labels.get(r["category"], r["category"]), "total": r["total"], "count": r["count"]}
        for r in rows
    ]
