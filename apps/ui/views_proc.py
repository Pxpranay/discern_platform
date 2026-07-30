"""Procurement and receipt screens."""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from apps.core.models import Location
from apps.inventory import services as inventory
from apps.inventory.models import GoodsReceipt
from apps.platform_core.exceptions import DomainError
from apps.platform_core.services import events
from apps.procurement import services as procurement
from apps.procurement.models import (
    Award,
    ProcurementRequest,
    PurchaseOrder,
    Rfq,
    RfqVendor,
    Vendor,
)

from .access import require_action, requires


def _dec(raw, field):
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise DomainError(f"{field} must be a number.")


@requires("procurement:view")
def procurement_home(request):
    return render(request, "ui/proc/index.html", {
        "requests": ProcurementRequest.objects.select_related("project")
        .annotate(line_count=Count("lines")).order_by("-created_at")[:40],
        "rfqs": Rfq.objects.select_related("request__project")
        .annotate(vendor_count=Count("vendors")).order_by("-created_at")[:25],
        "orders": PurchaseOrder.objects.select_related("vendor", "project")
        .order_by("-created_at")[:25],
        "vendors": Vendor.objects.filter(is_active=True)[:100],
        "nav": "procurement",
    })


@requires("procurement:view")
def request_detail(request, pk):
    pr = get_object_or_404(ProcurementRequest.objects.select_related("project"), pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "approve":
                require_action(request, "procurement:approve_request")
                procurement.approve_request(request=pr, actor=request.user)
                events.drain()
                messages.success(request, "Approved. Procurement can now source it.")
            elif action == "hold":
                require_action(request, "procurement:approve_request")
                procurement.hold_request(
                    request=pr, reason=request.POST.get("reason", ""), actor=request.user
                )
                messages.info(request, "Held for re-review.")
            elif action == "rfq":
                require_action(request, "procurement:rfq")
                vendor_ids = request.POST.getlist("vendors")
                rfq = procurement.create_rfq(
                    request=pr,
                    vendors=list(Vendor.objects.filter(pk__in=vendor_ids)),
                    actor=request.user,
                )
                messages.success(request, f"{rfq.number} created.")
                return redirect("rfq_detail", pk=rfq.pk)
        except DomainError as exc:
            messages.error(request, str(exc))
        return redirect("request_detail", pk=pr.pk)

    lines = []
    for line in pr.lines.select_related("boq_line", "item"):
        lines.append({
            "line": line,
            "availability": procurement.stock_availability(
                item_id=line.item_id, description=line.description
            ),
        })
    return render(request, "ui/proc/request_detail.html", {
        "pr": pr, "lines": lines,
        "vendors": Vendor.objects.filter(is_active=True),
        "rfqs": pr.rfqs.all(),
        "nav": "procurement",
    })


@requires("procurement:view")
def rfq_detail(request, pk):
    rfq = get_object_or_404(Rfq.objects.select_related("request__project"), pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "issue":
                require_action(request, "procurement:rfq")
                procurement.issue_rfq(rfq=rfq, actor=request.user)
                messages.success(request, "Issued to vendors.")
            elif action == "waive":
                require_action(request, "procurement:rfq")
                rfq.min_vendors_waived_reason = request.POST.get("reason", "")
                rfq.save(update_fields=["min_vendors_waived_reason"])
                messages.info(request, "Reason recorded.")
            elif action == "quote":
                require_action(request, "procurement:rfq")
                rfq_vendor = get_object_or_404(
                    RfqVendor, pk=request.POST.get("rfq_vendor"), rfq=rfq
                )
                quotes = []
                for line in rfq.request.lines.all():
                    raw = request.POST.get(f"rate_{line.pk}")
                    if raw:
                        quotes.append({"request_line": line, "rate": _dec(raw, "Rate")})
                if not quotes:
                    raise DomainError("Enter at least one rate.")
                procurement.record_quote(
                    rfq_vendor=rfq_vendor, quotes=quotes, actor=request.user
                )
                messages.success(request, f"Quote recorded for {rfq_vendor.vendor.name}.")
            elif action == "award":
                require_action(request, "procurement:award")
                line = rfq.request.lines.get(pk=request.POST.get("line"))
                vendor = Vendor.objects.get(pk=request.POST.get("vendor"))
                procurement.award_line(
                    rfq=rfq, request_line=line, vendor=vendor, actor=request.user,
                    notes=request.POST.get("notes", ""),
                )
                messages.success(request, f"Awarded to {vendor.name}.")
            elif action == "raise_po":
                require_action(request, "purchase_order:create")
                awards = list(Award.objects.filter(pk__in=request.POST.getlist("awards")))
                order = procurement.create_purchase_order(awards=awards, actor=request.user)
                messages.success(request, f"{order.number} drafted.")
                return redirect("po_detail", pk=order.pk)
        except (DomainError, Vendor.DoesNotExist) as exc:
            messages.error(request, str(exc))
        return redirect("rfq_detail", pk=rfq.pk)

    return render(request, "ui/proc/rfq_detail.html", {
        "rfq": rfq,
        "statement": procurement.comparison(rfq),
        "awards": rfq.awards.select_related("winning_vendor", "request_line"),
        "nav": "procurement",
    })


@requires("procurement:view")
def po_detail(request, pk):
    order = get_object_or_404(
        PurchaseOrder.objects.select_related("vendor", "project"), pk=pk
    )
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "submit":
                require_action(request, "purchase_order:create")
                procurement.submit_purchase_order(order=order, actor=request.user)
                events.drain()
                order.refresh_from_db()
                if order.status == PurchaseOrder.AWAITING_APPROVAL:
                    messages.info(
                        request,
                        "Above the value threshold — parked for the Purchase Manager.",
                    )
                else:
                    messages.success(request, "Confirmed. The receipt is now expected at site.")
            elif action == "approve":
                require_action(request, "purchase_order:approve")
                procurement.approve_purchase_order(order=order, actor=request.user)
                events.drain()
                messages.success(request, "Approved and confirmed.")
            elif action == "amend":
                require_action(request, "purchase_order:create")
                line = order.lines.get(pk=request.POST.get("line"))
                procurement.amend_line(
                    line=line, new_qty=_dec(request.POST.get("new_qty"), "Quantity"),
                    reason=request.POST.get("reason", ""), actor=request.user,
                )
                messages.success(request, "Line amended; the headroom is released.")
        except DomainError as exc:
            messages.error(request, str(exc))
        return redirect("po_detail", pk=order.pk)

    return render(request, "ui/proc/po_detail.html", {
        "order": order,
        "lines": order.lines.select_related("boq_line", "item"),
        "nav": "procurement",
    })


@requires("receipt:view")
def receipts(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "record":
                require_action(request, "receipt:record")
                from apps.procurement.models import PurchaseOrderLine

                line = PurchaseOrderLine.objects.get(pk=request.POST.get("line"))
                receipt = inventory.record_receipt(
                    purchase_order_line=line,
                    quantity=_dec(request.POST.get("quantity"), "Quantity"),
                    actor=request.user,
                    vendor_challan=request.POST.get("challan", ""),
                )
                messages.success(request, f"{receipt.number} recorded — awaiting verification.")
            elif action == "verify":
                require_action(request, "receipt:verify")
                receipt = GoodsReceipt.objects.get(pk=request.POST.get("receipt"))
                inventory.verify_receipt(
                    receipt=receipt,
                    accepted_qty=_dec(request.POST.get("accepted"), "Accepted quantity"),
                    rejected_qty=_dec(request.POST.get("rejected") or 0, "Rejected quantity"),
                    notes=request.POST.get("notes", ""),
                    actor=request.user,
                )
                events.drain()
                messages.success(request, "Verified — stock and cost posted.")
            elif action == "return":
                require_action(request, "receipt:return")
                receipt = GoodsReceipt.objects.get(pk=request.POST.get("receipt"))
                inventory.return_material(
                    purchase_order_line=receipt.purchase_order_line,
                    quantity=_dec(request.POST.get("quantity"), "Quantity"),
                    reason=request.POST.get("reason", ""),
                    actor=request.user, goods_receipt=receipt,
                )
                messages.success(request, "Returned — cost reversed and headroom released.")
        except (DomainError, GoodsReceipt.DoesNotExist) as exc:
            messages.error(request, str(exc))
        return redirect("receipts")

    from apps.inventory.models import ExpectedReceipt
    from apps.procurement.models import PurchaseOrderLine

    awaited = ExpectedReceipt.objects.select_related(
        "purchase_order_line__purchase_order__vendor", "location"
    ).exclude(status=ExpectedReceipt.COMPLETE)
    return render(request, "ui/proc/receipts.html", {
        "awaited": awaited,
        "recorded": GoodsReceipt.objects.select_related(
            "purchase_order_line__purchase_order__vendor", "location"
        ).filter(status=GoodsReceipt.RECORDED),
        "recent": GoodsReceipt.objects.select_related("verification", "location")
        .exclude(status=GoodsReceipt.RECORDED)[:20],
        "nav": "receipts",
    })
