"""Site expenses — the same cost ledger as everything else."""

from django.db import transaction
from django.utils import timezone

from apps.platform_core.exceptions import DomainError
from apps.platform_core.models import CostEntry
from apps.platform_core.services import costing, events

from .models import SiteExpense


@transaction.atomic
def submit_expense(*, project, category, amount, expense_date, actor, **extra) -> SiteExpense:
    if not actor.has_capability("expenses:submit"):
        raise DomainError(f"{actor} cannot submit a site expense.")
    return SiteExpense.objects.create(
        project=project, category=category, amount=amount, expense_date=expense_date,
        submitted_by=actor, status=SiteExpense.SUBMITTED, **extra,
    )


@transaction.atomic
def approve_expense(*, expense: SiteExpense, actor) -> SiteExpense:
    """Approve and post to the cost ledger.

    Unapproved claims still appear on the expense-vs-income view flagged as
    pending, per the decisions register: hiding real spend until it is approved
    makes the margin figure optimistic exactly when it matters.
    """
    if not actor.has_capability("expenses:approve"):
        raise DomainError(f"{actor} cannot approve a site expense.")
    if expense.status == SiteExpense.APPROVED:
        raise DomainError("Already approved.")

    expense.status = SiteExpense.APPROVED
    expense.approved_by = actor
    expense.approved_at = timezone.now()
    expense.save(update_fields=["status", "approved_by", "approved_at"])

    costing.post_cost(
        project_id=expense.project_id, lot_id=expense.lot_id,
        category=CostEntry.SITE_EXPENSE, amount=expense.amount,
        source_type="site_expense", source_id=expense.pk, actor=actor,
        effective_date=expense.expense_date,
    )
    events.emit("ExpenseApproved", {"expense_id": expense.pk})
    return expense


def expense_vs_income(project_id):
    """The Site Expense vs Income comparison sheet (process design §4.11).

    One query, two viewers — the Construction Manager and the Project Manager
    see identical numbers because there is only one set.
    """
    from apps.platform_core.models import CostEntry as CE

    entries = CE.objects.filter(project_id=project_id).order_by("effective_date")
    return {
        "expenses": entries.filter(category=CE.SITE_EXPENSE),
        "subcontract": entries.filter(category=CE.SUBCONTRACT),
        "revenue": entries.filter(category=CE.REVENUE),
        "pending": SiteExpense.objects.filter(
            project_id=project_id, status=SiteExpense.SUBMITTED
        ),
    }
