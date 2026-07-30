"""Fabrication workflow: cap the finished item, source what is short, produce."""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.models import Location
from apps.platform_core.exceptions import DomainError
from apps.platform_core.models import CostEntry
from apps.platform_core.services import costing, events
from apps.platform_core.services.ceiling import reserve_headroom
from apps.platform_core.services.stock import on_hand, post_move

from .models import (
    BillOfMaterials,
    FabricationMode,
    FabricationOrder,
    FabricationStep,
    MaterialConsumption,
)

ZERO = Decimal("0")


def _number(prefix, model):
    stamp = timezone.now().strftime("%y%m%d")
    n = model.objects.filter(number__startswith=f"{prefix}-{stamp}").count()
    return f"{prefix}-{stamp}-{n + 1:03d}"


def _works_location(project):
    location, _ = Location.objects.get_or_create(
        code=f"{project.code}-WORKS",
        defaults={"name": f"{project.name} works", "kind": Location.WORKS, "project": project},
    )
    return location


@transaction.atomic
def create_order(
    *, boq_line, quantity, actor, bom=None, mode=FabricationMode.IN_HOUSE,
    vendor=None, job_work_charge=None,
) -> FabricationOrder:
    """Raise a fabrication order, capped by the BOQ ceiling on the finished item."""
    if boq_line.route != boq_line.FABRICATE:
        raise DomainError(
            f"“{boq_line.description}” is routed {boq_line.route}, not FABRICATE."
        )
    if not actor.has_capability("fabrication:manage"):
        raise DomainError(f"{actor} cannot raise a fabrication order.")
    if mode == FabricationMode.JOB_WORK and vendor is None:
        raise DomainError("Job work needs a vendor to send the components to.")

    quantity = Decimal(str(quantity))
    bom = bom or BillOfMaterials.objects.filter(item=boq_line.item, is_active=True).first()

    order = FabricationOrder.objects.create(
        project=boq_line.project, lot=boq_line.lot, boq_line=boq_line,
        item=boq_line.item, bom=bom, number=_number("FO", FabricationOrder),
        quantity=quantity, uom=boq_line.uom, mode=mode, vendor=vendor,
        job_work_charge=job_work_charge,
    )
    reserve_headroom(
        boq_line_id=boq_line.pk, qty=quantity,
        document_type="fabrication_order", document_id=order.pk,
        actor=actor, reason=f"{order.number} to fabricate {boq_line.description}",
    )

    _works_location(order.project)  # provisioned up front, not on first query

    if bom:
        for component in bom.components.select_related("item"):
            MaterialConsumption.objects.create(
                order=order, item=component.item,
                planned_qty=component.required_for(quantity), uom=component.uom,
            )
    events.emit("FabricationOrderCreated", {"order_id": order.pk})
    return order


def material_shortfall(order: FabricationOrder) -> list[dict]:
    """What the recipe needs beyond what is on hand at the works."""
    location = _works_location(order.project)
    rows = []
    for line in order.consumption.select_related("item"):
        available = on_hand(line.item_id, location.pk)
        if line.planned_qty > available:
            rows.append({
                "item": line.item, "required": line.planned_qty,
                "available": available, "short": line.planned_qty - available,
                "uom": line.uom,
            })
    return rows


@transaction.atomic
def request_shortfall(*, order: FabricationOrder, actor):
    """Raise child procurement requests for the missing raw materials.

    Deliberately **not** ceiling-checked: these are components consumed to
    produce the BOQ line, not the line's own item. The finished quantity was
    already capped when the order was created, so the ceiling sits upstream.
    """
    from apps.procurement.models import ProcurementRequest, ProcurementRequestLine, RequestSource
    from apps.procurement.services import _number as pr_number

    shortfall = material_shortfall(order)
    if not shortfall:
        return None

    request = ProcurementRequest.objects.create(
        project=order.project, source=RequestSource.FABRICATION_SHORTFALL,
        number=pr_number("PR", ProcurementRequest),
        status=ProcurementRequest.APPROVED,
        requested_by=actor,
        notes=f"Raw material short for {order.number}",
    )
    for row in shortfall:
        ProcurementRequestLine.objects.create(
            request=request, item=row["item"], description=row["item"].name,
            quantity=row["short"], uom=row["uom"],
        )
    order.status = FabricationOrder.AWAITING_MATERIAL
    order.save(update_fields=["status"])
    return request


@transaction.atomic
def start(*, order: FabricationOrder, actor) -> FabricationOrder:
    if material_shortfall(order):
        raise DomainError(
            f"{order.number} is short of raw material. Source it before starting."
        )
    order.status = FabricationOrder.IN_PROGRESS
    order.started_at = timezone.now()
    order.save(update_fields=["status", "started_at"])

    if order.is_job_work:
        # Components leave for the vendor's premises and come back as the item.
        vendor_location, _ = Location.objects.get_or_create(
            code=f"VENDOR-{order.vendor_id}",
            defaults={"name": order.vendor.name, "kind": Location.VENDOR},
        )
        works = _works_location(order.project)
        for line in order.consumption.select_related("item"):
            post_move(
                item_id=line.item_id, quantity=line.planned_qty,
                from_location_id=works.pk, to_location_id=vendor_location.pk,
                source_type="job_work_issue", source_id=order.pk, actor=actor,
            )
    return order


@transaction.atomic
def complete(*, order: FabricationOrder, actor, actual_consumption: dict | None = None,
             unit_cost=None) -> FabricationOrder:
    """Finish the run: consume components, produce the item, post cost."""
    if order.status not in (FabricationOrder.IN_PROGRESS, FabricationOrder.AWAITING_MATERIAL):
        raise DomainError(f"{order.number} is {order.get_status_display()}.")

    works = _works_location(order.project)
    source_location = works
    if order.is_job_work:
        source_location = Location.objects.get(code=f"VENDOR-{order.vendor_id}")

    material_cost = ZERO
    for line in order.consumption.select_related("item"):
        actual = Decimal(str((actual_consumption or {}).get(line.item_id, line.planned_qty)))
        line.actual_qty = actual
        line.save(update_fields=["actual_qty"])
        if actual > ZERO:
            post_move(
                item_id=line.item_id, quantity=actual,
                from_location_id=source_location.pk,
                source_type="fabrication_consumption", source_id=order.pk, actor=actor,
            )

    site = Location.objects.filter(
        project=order.project, kind=Location.SITE
    ).first() or works
    post_move(
        item_id=order.item_id, boq_line_id=order.boq_line_id, quantity=order.quantity,
        to_location_id=site.pk, unit_value=unit_cost,
        source_type="fabrication_output", source_id=order.pk, actor=actor,
    )

    amount = (
        Decimal(str(unit_cost)) * order.quantity if unit_cost is not None
        else (order.job_work_charge or ZERO)
    )
    if amount > ZERO:
        costing.post_cost(
            project_id=order.project_id, lot_id=order.lot_id, boq_line_id=order.boq_line_id,
            category=CostEntry.FABRICATION, amount=amount,
            source_type="fabrication_order", source_id=order.pk, actor=actor,
        )

    order.status = FabricationOrder.COMPLETE
    order.completed_at = timezone.now()
    order.save(update_fields=["status", "completed_at"])
    order.lock(actor)
    events.emit("FabricationCompleted", {"order_id": order.pk})
    return order


@transaction.atomic
def complete_step(*, step: FabricationStep, actor) -> FabricationStep:
    step.completed_by = actor
    step.completed_at = timezone.now()
    step.save(update_fields=["completed_by", "completed_at"])
    return step
