"""Fabrication, subcontract and site-expense screens."""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from apps.core.models import BoqLine, Item, Location
from apps.fabrication import services as fab
from apps.fabrication.models import BillOfMaterials, FabricationMode, FabricationOrder
from apps.finance import services as finance
from apps.finance.models import ExpenseCategory, SiteExpense
from apps.inventory import services as inventory
from apps.inventory.models import ExcessStockFlag, GoodsReceipt, StockTransfer
from apps.platform_core.exceptions import DomainError
from apps.platform_core.services import events
from apps.procurement.models import Vendor
from apps.subcontracts import services as subs
from apps.subcontracts.models import ServiceOrder

from .access import require_action, requires


def _dec(raw, field):
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise DomainError(f"{field} must be a number.")


@requires("works:view")
def works(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "fabricate":
                require_action(request, "fabrication:manage")
                line = BoqLine.objects.get(pk=request.POST.get("boq_line"))
                mode = request.POST.get("mode") or FabricationMode.IN_HOUSE
                order = fab.create_order(
                    boq_line=line, quantity=_dec(request.POST.get("quantity"), "Quantity"),
                    actor=request.user, mode=mode,
                    vendor=Vendor.objects.filter(pk=request.POST.get("vendor") or 0).first(),
                    job_work_charge=_dec(request.POST["charge"], "Charge")
                    if request.POST.get("charge") else None,
                )
                messages.success(request, f"{order.number} raised.")
            elif action == "shortfall":
                require_action(request, "fabrication:manage")
                order = FabricationOrder.objects.get(pk=request.POST.get("order"))
                pr = fab.request_shortfall(order=order, actor=request.user)
                messages.success(
                    request,
                    f"{pr.number} raised for the missing raw material." if pr
                    else "No shortfall — everything is in stock.",
                )
            elif action == "start_fab":
                require_action(request, "fabrication:manage")
                fab.start(order=FabricationOrder.objects.get(pk=request.POST.get("order")),
                          actor=request.user)
                messages.success(request, "Production started.")
            elif action == "complete_fab":
                require_action(request, "fabrication:manage")
                fab.complete(
                    order=FabricationOrder.objects.get(pk=request.POST.get("order")),
                    actor=request.user,
                    unit_cost=_dec(request.POST["unit_cost"], "Unit cost")
                    if request.POST.get("unit_cost") else None,
                )
                events.drain()
                messages.success(request, "Complete — item in stock, cost posted.")
            elif action == "service_order":
                require_action(request, "service_order:issue")
                line = BoqLine.objects.get(pk=request.POST.get("boq_line"))
                order = subs.create_service_order(
                    boq_line=line,
                    vendor=Vendor.objects.get(pk=request.POST.get("vendor")),
                    quantity=_dec(request.POST.get("quantity"), "Quantity"),
                    actor=request.user,
                )
                subs.issue(order=order, actor=request.user)
                messages.success(request, f"{order.number} raised.")
            elif action == "progress":
                require_action(request, "service_order:progress")
                subs.log_progress(
                    order=ServiceOrder.objects.get(pk=request.POST.get("order")),
                    percent=_dec(request.POST.get("percent"), "Percent"),
                    actor=request.user, notes=request.POST.get("notes", ""),
                )
                messages.success(request, "Progress logged. This does not release billing.")
            elif action == "certify":
                require_action(request, "service_order:certify")
                certification = subs.certify(
                    order=ServiceOrder.objects.get(pk=request.POST.get("order")),
                    quantity=_dec(request.POST.get("quantity"), "Quantity"),
                    actor=request.user, is_final=bool(request.POST.get("final")),
                )
                events.drain()
                messages.success(
                    request, f"Running bill RA-{certification.running_bill_number} certified."
                )
            elif action == "approve_so":
                require_action(request, "purchase_order:approve")
                subs.approve(order=ServiceOrder.objects.get(pk=request.POST.get("order")),
                             actor=request.user)
                messages.success(request, "Approved and issued.")
        except (DomainError, Vendor.DoesNotExist, BoqLine.DoesNotExist) as exc:
            messages.error(request, str(exc))
        return redirect("works")

    return render(request, "ui/works/index.html", {
        "fab_orders": FabricationOrder.objects.select_related("project", "item", "vendor")[:25],
        "service_orders": ServiceOrder.objects.select_related("project", "vendor")[:25],
        "fab_lines": BoqLine.objects.filter(route=BoqLine.FABRICATE).select_related("project")[:50],
        "service_lines": BoqLine.objects.filter(route=BoqLine.SERVICE).select_related("project")[:50],
        "vendors": Vendor.objects.filter(is_active=True),
        "modes": FabricationMode.choices,
        "nav": "works",
    })


@requires("expenses:view")
def expenses(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "submit":
                require_action(request, "expenses:submit")
                from apps.core.models import Project

                finance.submit_expense(
                    project=Project.objects.get(pk=request.POST.get("project")),
                    category=request.POST.get("category"),
                    amount=_dec(request.POST.get("amount"), "Amount"),
                    expense_date=request.POST.get("expense_date"),
                    actor=request.user,
                    description=request.POST.get("description", ""),
                )
                messages.success(request, "Expense submitted.")
            elif action == "approve":
                require_action(request, "expenses:approve")
                finance.approve_expense(
                    expense=SiteExpense.objects.get(pk=request.POST.get("expense")),
                    actor=request.user,
                )
                events.drain()
                messages.success(request, "Approved — posted to the project's cost.")
        except (DomainError, SiteExpense.DoesNotExist) as exc:
            messages.error(request, str(exc))
        return redirect("expenses")

    from apps.core.models import Project

    return render(request, "ui/works/expenses.html", {
        "pending": SiteExpense.objects.filter(status=SiteExpense.SUBMITTED)
        .select_related("project", "submitted_by"),
        "approved": SiteExpense.objects.filter(status=SiteExpense.APPROVED)
        .select_related("project")[:30],
        "projects": Project.objects.filter(is_active=True),
        "categories": ExpenseCategory.choices,
        "nav": "expenses",
    })


@requires("receipt:view")
def excess_stock(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "flag":
                require_action(request, "stock:flag_excess")
                receipt = GoodsReceipt.objects.get(pk=request.POST.get("receipt"))
                inventory.flag_excess(
                    item=receipt.purchase_order_line.item, location=receipt.location,
                    quantity=_dec(request.POST.get("quantity"), "Quantity"),
                    actor=request.user, goods_receipt=receipt,
                    reason=request.POST.get("reason") or ExcessStockFlag.AVAILABLE,
                    notes=request.POST.get("notes", ""),
                )
                events.drain()
                messages.success(request, "Flagged — three dashboards notified.")
            elif action == "redeploy":
                require_action(request, "stock:transfer")
                transfer = inventory.redeploy(
                    flag=ExcessStockFlag.objects.get(pk=request.POST.get("flag")),
                    to_location=Location.objects.get(pk=request.POST.get("to_location")),
                    actor=request.user, reason=request.POST.get("reason", ""),
                )
                messages.success(
                    request, f"{transfer.number} proposed — the receiving PM must accept."
                )
            elif action == "accept":
                require_action(request, "stock:transfer")
                inventory.accept_transfer(
                    transfer=StockTransfer.objects.get(pk=request.POST.get("transfer")),
                    actor=request.user,
                )
                events.drain()
                messages.success(request, "Transferred — value moved between both projects.")
            elif action == "decline":
                require_action(request, "stock:transfer")
                inventory.decline_transfer(
                    transfer=StockTransfer.objects.get(pk=request.POST.get("transfer")),
                    actor=request.user, reason=request.POST.get("reason", ""),
                )
                messages.info(request, "Declined.")
        except (DomainError, GoodsReceipt.DoesNotExist, ExcessStockFlag.DoesNotExist) as exc:
            messages.error(request, str(exc))
        return redirect("excess_stock")

    return render(request, "ui/works/excess.html", {
        "flags": ExcessStockFlag.objects.select_related("item", "project", "location")[:30],
        "transfers": StockTransfer.objects.select_related(
            "item", "from_location", "to_location"
        )[:30],
        "receipts": GoodsReceipt.objects.filter(status=GoodsReceipt.VERIFIED)
        .select_related("purchase_order_line__item", "location")[:30],
        "locations": Location.objects.filter(kind=Location.SITE).select_related("project"),
        "reasons": ExcessStockFlag.REASON_CHOICES,
        "nav": "receipts",
    })
