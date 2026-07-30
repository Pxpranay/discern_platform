"""Role dashboards and the mobile site screens."""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from apps.core.models import Project
from apps.finance import services as finance
from apps.finance.models import ExpenseCategory
from apps.inventory import services as inventory
from apps.inventory.models import ExpectedReceipt, GoodsReceipt
from apps.platform_core.exceptions import DomainError
from apps.platform_core.services import events
from apps.reporting import services as reporting
from apps.subcontracts import services as subs
from apps.subcontracts.models import ServiceOrder

from .access import require_action, requires


def _dec(raw, field):
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise DomainError(f"{field} must be a number.")


@requires("projects:view")
def pm_dashboard(request, pk):
    project = get_object_or_404(Project.objects.select_related("client"), pk=pk)
    data = reporting.project_dashboard(project)
    data["nav"] = "projects"
    return render(request, "ui/dash/project.html", data)


@requires("procurement:view")
def purchase_dashboard(request):
    data = reporting.purchase_dashboard()
    data["nav"] = "procurement"
    return render(request, "ui/dash/purchase.html", data)


@requires("dashboard:view")
def portfolio(request):
    data = reporting.portfolio()
    data["nav"] = "dashboard"
    return render(request, "ui/dash/portfolio.html", data)


@requires("expenses:view")
def expense_sheet(request, pk):
    """The same query the Project Manager sees. One set of numbers."""
    project = get_object_or_404(Project, pk=pk)
    data = reporting.expense_vs_income(project)
    data["nav"] = "expenses"
    return render(request, "ui/dash/expense_sheet.html", data)


# ------------------------------------------------------------- mobile
@requires("receipt:view")
def m_home(request):
    return render(request, "ui/m/home.html", {
        "awaited": ExpectedReceipt.objects.exclude(
            status=ExpectedReceipt.COMPLETE
        ).select_related("purchase_order_line__purchase_order__vendor", "location")[:20],
        "to_verify": GoodsReceipt.objects.filter(status=GoodsReceipt.RECORDED)
        .select_related("purchase_order_line")[:20],
        "orders": ServiceOrder.objects.filter(status=ServiceOrder.ISSUED)[:20],
        "projects": Project.objects.filter(is_active=True)[:50],
        "categories": ExpenseCategory.choices,
    })


@requires("receipt:view")
def m_receive(request, pk):
    expected = get_object_or_404(
        ExpectedReceipt.objects.select_related(
            "purchase_order_line__purchase_order__vendor", "location"
        ), pk=pk,
    )
    if request.method == "POST":
        try:
            require_action(request, "receipt:record")
            receipt = inventory.record_receipt(
                purchase_order_line=expected.purchase_order_line,
                quantity=_dec(request.POST.get("quantity"), "Quantity"),
                actor=request.user, vendor_challan=request.POST.get("challan", ""),
            )
            messages.success(request, f"{receipt.number} recorded.")
            return redirect("m_verify", pk=receipt.pk)
        except DomainError as exc:
            messages.error(request, str(exc))
        return redirect("m_receive", pk=expected.pk)

    return render(request, "ui/m/receive.html", {"expected": expected})


@requires("receipt:view")
def m_verify(request, pk):
    receipt = get_object_or_404(
        GoodsReceipt.objects.select_related("purchase_order_line", "location"), pk=pk
    )
    if request.method == "POST":
        try:
            require_action(request, "receipt:verify")
            inventory.verify_receipt(
                receipt=receipt,
                accepted_qty=_dec(request.POST.get("accepted"), "Accepted"),
                rejected_qty=_dec(request.POST.get("rejected") or 0, "Rejected"),
                notes=request.POST.get("notes", ""), actor=request.user,
            )
            events.drain()
            messages.success(request, "Verified. Stock and cost posted.")
            return redirect("m_home")
        except DomainError as exc:
            messages.error(request, str(exc))
        return redirect("m_verify", pk=receipt.pk)

    return render(request, "ui/m/verify.html", {"receipt": receipt})


@requires("works:view")
def m_progress(request, pk):
    order = get_object_or_404(ServiceOrder.objects.select_related("vendor", "project"), pk=pk)
    if request.method == "POST":
        try:
            require_action(request, "service_order:progress")
            subs.log_progress(
                order=order, percent=_dec(request.POST.get("percent"), "Percent"),
                actor=request.user, notes=request.POST.get("notes", ""),
            )
            messages.success(request, "Progress logged.")
            return redirect("m_home")
        except DomainError as exc:
            messages.error(request, str(exc))
        return redirect("m_progress", pk=order.pk)

    return render(request, "ui/m/progress.html", {
        "order": order, "recent": order.progress.all()[:5],
    })


@requires("expenses:view")
def m_expense(request):
    if request.method == "POST":
        try:
            require_action(request, "expenses:submit")
            finance.submit_expense(
                project=Project.objects.get(pk=request.POST.get("project")),
                category=request.POST.get("category"),
                amount=_dec(request.POST.get("amount"), "Amount"),
                expense_date=request.POST.get("expense_date"),
                actor=request.user, description=request.POST.get("description", ""),
            )
            messages.success(request, "Expense submitted.")
            return redirect("m_home")
        except (DomainError, Project.DoesNotExist) as exc:
            messages.error(request, str(exc))
        return redirect("m_expense")

    return render(request, "ui/m/expense.html", {
        "projects": Project.objects.filter(is_active=True),
        "categories": ExpenseCategory.choices,
    })
