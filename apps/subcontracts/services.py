"""Service orders direct to empanelled subcontractors, and certification."""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.platform_core.exceptions import DomainError
from apps.platform_core.models import CostEntry
from apps.platform_core.services import costing, events
from apps.platform_core.services.ceiling import release_headroom, reserve_headroom
from apps.procurement.models import VendorRate

from .models import ServiceCertification, ServiceOrder, ServiceProgress, VendorBill

ZERO = Decimal("0")
DEFAULT_APPROVAL_THRESHOLD = Decimal("500000")


def _number(prefix, model):
    stamp = timezone.now().strftime("%y%m%d")
    n = model.objects.filter(number__startswith=f"{prefix}-{stamp}").count()
    return f"{prefix}-{stamp}-{n + 1:03d}"


def agreed_rate(vendor, boq_line):
    """The rate that lets a service order skip price discovery."""
    rate = VendorRate.objects.filter(vendor=vendor, item=boq_line.item).first()
    if rate is None:
        rate = VendorRate.objects.filter(
            vendor=vendor, description__iexact=boq_line.description
        ).first()
    return rate.rate if rate else None


@transaction.atomic
def create_service_order(*, boq_line, vendor, quantity, actor, rate=None, scope="") -> ServiceOrder:
    """Raise a service order straight from a released BOQ service line.

    No RFQ round: Discern has empanelled subcontractors on agreed rates, and
    floating a tender for every service line would slow things down for no
    benefit. A vendor with neither empanelment nor an agreed rate is refused
    here and routed through normal procurement instead — that is exactly when
    price discovery has value.
    """
    if boq_line.route != boq_line.SERVICE:
        raise DomainError(
            f"“{boq_line.description}” is routed {boq_line.route}, not SERVICE."
        )
    if not actor.has_capability("service_order:issue"):
        raise DomainError(f"{actor} cannot issue a service order.")

    resolved = rate if rate is not None else agreed_rate(vendor, boq_line)
    if resolved is None:
        raise DomainError(
            f"{vendor.name} has no agreed rate for this scope. A new or one-off "
            f"subcontractor should go through a normal RFQ."
        )
    if not vendor.is_empanelled and rate is None:
        raise DomainError(
            f"{vendor.name} is not empanelled. Route them through a normal RFQ, "
            f"or record an agreed rate first."
        )

    quantity = Decimal(str(quantity))
    order = ServiceOrder.objects.create(
        project=boq_line.project, lot=boq_line.lot, boq_line=boq_line, vendor=vendor,
        number=_number("SO", ServiceOrder),
        scope_description=scope or boq_line.description,
        quantity=quantity, uom=boq_line.uom, rate=Decimal(str(resolved)),
    )
    reserve_headroom(
        boq_line_id=boq_line.pk, qty=quantity,
        document_type="service_order", document_id=order.pk,
        actor=actor, reason=f"{order.number} to {vendor.name}",
    )
    return order


@transaction.atomic
def issue(*, order: ServiceOrder, actor, threshold=None) -> ServiceOrder:
    """Send it to the subcontractor, or park it above the value threshold.

    A directly-issued service order is not a route around the approval
    threshold — the same second signature applies as to any purchase order.
    """
    if order.status == ServiceOrder.ISSUED:
        raise DomainError(f"{order.number} is already issued.")

    threshold = threshold if threshold is not None else DEFAULT_APPROVAL_THRESHOLD
    if order.total_value > threshold and not actor.has_capability("purchase_order:approve"):
        order.status = ServiceOrder.AWAITING_APPROVAL
        order.save(update_fields=["status"])
        return order

    order.status = ServiceOrder.ISSUED
    order.issued_at = timezone.now()
    order.save(update_fields=["status", "issued_at"])
    order.lock(actor)
    events.emit("ServiceOrderIssued", {"order_id": order.pk})
    return order


@transaction.atomic
def approve(*, order: ServiceOrder, actor) -> ServiceOrder:
    if not actor.has_capability("purchase_order:approve"):
        raise DomainError(f"{actor} cannot approve a service order.")
    if order.status != ServiceOrder.AWAITING_APPROVAL:
        raise DomainError(f"{order.number} is {order.get_status_display()}.")
    return issue(order=order, actor=actor, threshold=Decimal("0") - Decimal("1"))


@transaction.atomic
def log_progress(*, order: ServiceOrder, percent, actor, quantity_done=None, notes="") -> ServiceProgress:
    """Anyone with visibility may report. Reporting is not certifying."""
    if not actor.has_capability("service_order:progress"):
        raise DomainError(f"{actor} cannot log progress.")
    return ServiceProgress.objects.create(
        service_order=order, reported_by=actor,
        percent_complete=Decimal(str(percent)),
        quantity_done=Decimal(str(quantity_done)) if quantity_done is not None else None,
        notes=notes,
    )


@transaction.atomic
def certify(*, order: ServiceOrder, quantity, actor, is_final=False, notes="") -> ServiceCertification:
    """The gate that releases billing, and posts SUBCONTRACT cost.

    No goods receipt: there is nothing physical to receive. Running-bill
    certification is expected — a subcontractor bills progressively, and each
    certified stage bills independently.
    """
    if not actor.has_capability("service_order:certify"):
        raise DomainError(f"{actor} cannot certify completed work.")
    if order.status not in (ServiceOrder.ISSUED, ServiceOrder.COMPLETE):
        raise DomainError(f"{order.number} is {order.get_status_display()}.")

    quantity = Decimal(str(quantity))
    if quantity <= ZERO:
        raise DomainError("A certification needs a positive quantity.")
    if quantity > order.outstanding_qty:
        raise DomainError(
            f"{order.outstanding_qty} {order.uom} remains uncertified on "
            f"{order.number}; {quantity} was certified."
        )

    bill_number = order.certifications.count() + 1
    value = quantity * order.rate
    certification = ServiceCertification.objects.create(
        service_order=order, running_bill_number=bill_number,
        certified_quantity=quantity, certified_value=value,
        is_final=is_final, certified_by=actor, certified_at=timezone.now(), notes=notes,
    )

    VendorBill.objects.create(
        vendor=order.vendor, project=order.project, lot=order.lot,
        number=f"{order.number}-RA{bill_number}",
        source_type="service_certification", source_id=certification.pk,
        bill_date=timezone.now().date(), amount=value, status=VendorBill.POSTED,
    )
    costing.post_cost(
        project_id=order.project_id, lot_id=order.lot_id, boq_line_id=order.boq_line_id,
        category=CostEntry.SUBCONTRACT, amount=value,
        source_type="service_certification", source_id=certification.pk, actor=actor,
    )

    if is_final or order.outstanding_qty <= ZERO:
        # Scope closed short releases the balance back to the BOQ.
        if is_final and order.outstanding_qty > ZERO:
            release_headroom(
                boq_line_id=order.boq_line_id, qty=order.outstanding_qty,
                document_type="service_order", document_id=order.pk,
                actor=actor, reason=f"{order.number} closed final, short of ordered scope",
            )
        order.status = ServiceOrder.COMPLETE
        order.save(update_fields=["status"])

    events.emit("ServiceProgressCertified", {"certification_id": certification.pk})
    return certification
