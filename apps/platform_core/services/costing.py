"""Posting to the cost ledger.

Corrections are reversals (design principle 2). ``reverse`` posts a
compensating entry carrying its own reason and author; nothing is ever edited,
so a project's cost history always shows what actually happened.
"""

from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from ..models import CostEntry


def post_cost(
    *,
    project_id: int,
    category: str,
    amount: Decimal,
    source_type: str,
    source_id: int,
    actor,
    effective_date=None,
    lot_id: int | None = None,
    boq_line_id: int | None = None,
) -> CostEntry:
    return CostEntry.objects.create(
        project_id=project_id,
        lot_id=lot_id,
        boq_line_id=boq_line_id,
        category=category,
        amount=Decimal(amount),
        source_type=source_type,
        source_id=source_id,
        effective_date=effective_date or timezone.now().date(),
        actor=actor,
    )


def reverse_cost(entry: CostEntry, *, actor, effective_date=None) -> CostEntry:
    """Post the opposite of an entry, linked back to it."""
    return CostEntry.objects.create(
        project_id=entry.project_id,
        lot_id=entry.lot_id,
        boq_line_id=entry.boq_line_id,
        category=entry.category,
        amount=-entry.amount,
        source_type=entry.source_type,
        source_id=entry.source_id,
        effective_date=effective_date or timezone.now().date(),
        reverses=entry,
        actor=actor,
    )


def project_total(project_id: int, category: str | None = None) -> Decimal:
    qs = CostEntry.objects.filter(project_id=project_id)
    if category:
        qs = qs.filter(category=category)
    return qs.aggregate(total=Sum("amount"))["total"] or Decimal("0")


def profitability(project_id: int) -> dict:
    """Revenue less cost, plus the breakdown by category.

    One query per grouping over one table — which is the whole point of
    everything converging on the cost ledger (process design §4.13).
    """
    rows = (
        CostEntry.objects.filter(project_id=project_id)
        .values("category")
        .annotate(total=Sum("amount"))
    )
    by_category = {r["category"]: r["total"] or Decimal("0") for r in rows}
    revenue = by_category.get(CostEntry.REVENUE, Decimal("0"))
    cost = sum(
        (v for k, v in by_category.items() if k != CostEntry.REVENUE), Decimal("0")
    )
    return {
        "revenue": revenue,
        "cost": cost,
        "margin": revenue - cost,
        "by_category": by_category,
    }


def lot_profitability(lot_id: int) -> dict:
    """The same computation sliced by lot.

    Costs nothing extra to provide: every entry already carries its lot, which
    is why per-SITC-lot margin is a query rather than a reporting project.
    """
    rows = (
        CostEntry.objects.filter(lot_id=lot_id).values("category").annotate(total=Sum("amount"))
    )
    by_category = {r["category"]: r["total"] or Decimal("0") for r in rows}
    revenue = by_category.get(CostEntry.REVENUE, Decimal("0"))
    cost = sum((v for k, v in by_category.items() if k != CostEntry.REVENUE), Decimal("0"))
    return {"revenue": revenue, "cost": cost, "margin": revenue - cost}
