"""Procurement workflow.

The chain: a released BOQ revision (or a site requisition, or a fabrication
shortfall) becomes a procurement request → stock availability is checked →
an RFQ goes to at least three vendors → a comparison statement → the Purchase
Manager awards at their own discretion → a purchase order, checked against the
BOQ ceiling and approved above a threshold.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from apps.core.models import BoqLine, Location
from apps.engineering.models import BoqRevision, ReconciliationOutcome
from apps.platform_core.exceptions import DomainError
from apps.platform_core.services import events
from apps.platform_core.services.ceiling import headroom, release_headroom, reserve_headroom
from apps.platform_core.services.stock import availability

from .models import (
    Award,
    ProcurementRequest,
    ProcurementRequestLine,
    PurchaseOrder,
    PurchaseOrderLine,
    RequestSource,
    Rfq,
    RfqQuoteLine,
    RfqVendor,
    Vendor,
)

ZERO = Decimal("0")


def _number(prefix: str, model, field: str = "number") -> str:
    stamp = timezone.now().strftime("%y%m%d")
    count = model.objects.filter(**{f"{field}__startswith": f"{prefix}-{stamp}"}).count()
    return f"{prefix}-{stamp}-{count + 1:03d}"


# ------------------------------------------------------- requests (3 sources)
@transaction.atomic
def create_request_from_revision(*, revision: BoqRevision, actor) -> ProcurementRequest | None:
    """Turn a released revision's deltas into one request.

    Only outcomes asking for quantity become lines. The five other verdicts —
    quiet reductions, order amendments, returns — are handled elsewhere and must
    never reach Procurement as something to buy.
    """
    outcomes = revision.reconciliation_outcomes.filter(
        action=ReconciliationOutcome.REQUEST_DELTA
    )
    if not outcomes.exists():
        return None

    request = ProcurementRequest.objects.create(
        project=revision.project,
        source=RequestSource.BOQ_RELEASE,
        boq_revision=revision,
        number=_number("PR", ProcurementRequest),
        status=ProcurementRequest.APPROVED,  # the revision's release is the approval
        requested_by=actor or revision.released_by,
        approved_by=revision.released_by,
        approved_at=timezone.now(),
        notes=f"Auto-drafted from {revision}",
    )
    for outcome in outcomes:
        quantity = outcome.delta if outcome.delta > ZERO else outcome.new_qty
        ProcurementRequestLine.objects.create(
            request=request,
            boq_line=outcome.boq_line,
            item=outcome.boq_line.item if outcome.boq_line else None,
            description=outcome.description,
            quantity=quantity,
            uom=outcome.uom,
        )
    events.emit(
        "ProcurementRequestApproved",
        {"request_id": request.pk},
        idempotency_key=f"ProcurementRequestApproved:{request.pk}",
    )
    return request


@events.handles("BoqRevisionReleased")
def _request_deltas_on_release(payload: dict) -> None:
    revision = BoqRevision.objects.get(pk=payload["revision_id"])
    if revision.procurement_requests.exists():
        return  # at-least-once delivery
    create_request_from_revision(revision=revision, actor=revision.released_by)


@transaction.atomic
def raise_site_requisition(*, project, lines: list[dict], actor, notes: str = "") -> ProcurementRequest:
    """The Construction team raising a need from what they can see on site.

    Bound by the same ceiling as every other route: this is a shortcut to
    requisitioning sooner, not a way around the BOQ.
    """
    if not lines:
        raise DomainError("A requisition needs at least one line.")

    request = ProcurementRequest.objects.create(
        project=project,
        source=RequestSource.SITE_REQUISITION,
        number=_number("PR", ProcurementRequest),
        status=ProcurementRequest.AWAITING_APPROVAL,
        is_site_raised=True,
        requested_by=actor,
        notes=notes,
    )
    for line in lines:
        boq_line = line.get("boq_line")
        quantity = Decimal(str(line["quantity"]))
        if boq_line is not None:
            available = headroom(boq_line.pk)
            if quantity > available:
                raise DomainError(
                    f"“{line['description']}”: BOQ allows {available} more, "
                    f"but {quantity} was requested. A revision must raise it first."
                )
        ProcurementRequestLine.objects.create(
            request=request,
            boq_line=boq_line,
            item=line.get("item"),
            description=line["description"],
            quantity=quantity,
            uom=line.get("uom", "nos"),
            required_by=line.get("required_by"),
        )
    return request


@transaction.atomic
def approve_request(*, request: ProcurementRequest, actor) -> ProcurementRequest:
    """The Project Manager's gate on a site-raised requisition."""
    if request.status != ProcurementRequest.AWAITING_APPROVAL:
        raise DomainError(f"Request {request.number} is {request.get_status_display()}.")
    if not actor.has_capability("procurement:approve_request"):
        raise DomainError(f"{actor} cannot approve a site requisition.")

    request.status = ProcurementRequest.APPROVED
    request.approved_by = actor
    request.approved_at = timezone.now()
    request.save(update_fields=["status", "approved_by", "approved_at"])
    events.emit("ProcurementRequestApproved", {"request_id": request.pk})
    return request


@transaction.atomic
def hold_request(*, request: ProcurementRequest, reason: str, actor) -> ProcurementRequest:
    """A 'no' parks the request for re-review rather than discarding it."""
    request.status = ProcurementRequest.HELD
    request.hold_reason = reason
    request.save(update_fields=["status", "hold_reason"])
    return request


# ------------------------------------------------------- stock availability
def last_purchase(item_id=None, description: str = "") -> dict:
    """Last confirmed purchase price and date for an item."""
    qs = PurchaseOrderLine.objects.filter(
        purchase_order__status=PurchaseOrder.CONFIRMED
    )
    qs = qs.filter(item_id=item_id) if item_id else qs.filter(description=description)
    row = qs.aggregate(last_at=Max("purchase_order__confirmed_at"))
    if not row["last_at"]:
        return {"rate": None, "at": None, "vendor": None}
    line = qs.order_by("-purchase_order__confirmed_at").select_related(
        "purchase_order__vendor"
    ).first()
    return {"rate": line.rate, "at": row["last_at"], "vendor": line.purchase_order.vendor}


def stock_availability(*, item_id=None, description: str = "") -> dict:
    """What the Purchase Manager sees before any RFQ goes out.

    On-hand in every location Discern operates, plus the last purchase price and
    date. A query over the ledgers — nothing cached into staleness.
    """
    rows = []
    if item_id:
        locations = {loc.pk: loc for loc in Location.objects.all()}
        for row in availability(item_id):
            location = locations.get(row["location_id"])
            rows.append(
                {
                    "location": location,
                    "project": location.project if location else None,
                    "on_hand": row["on_hand"],
                }
            )
    return {
        "locations": [r for r in rows if r["on_hand"] != ZERO],
        "total_on_hand": sum((r["on_hand"] for r in rows), ZERO),
        "last_purchase": last_purchase(item_id=item_id, description=description),
    }


# --------------------------------------------------------------------- RFQ
@transaction.atomic
def create_rfq(*, request: ProcurementRequest, vendors: list[Vendor], actor, closes_at=None) -> Rfq:
    if request.status != ProcurementRequest.APPROVED:
        raise DomainError(
            f"Request {request.number} is {request.get_status_display()} — "
            f"only an approved request can be sourced."
        )
    rfq = Rfq.objects.create(
        request=request, number=_number("RFQ", Rfq), closes_at=closes_at
    )
    for vendor in vendors:
        RfqVendor.objects.create(rfq=rfq, vendor=vendor)
    return rfq


@transaction.atomic
def issue_rfq(*, rfq: Rfq, actor) -> Rfq:
    if rfq.vendors.count() < Rfq.MINIMUM_VENDORS and not rfq.min_vendors_waived_reason.strip():
        raise DomainError(
            f"{rfq.number} has {rfq.vendors.count()} vendor(s). Discern's rule is at "
            f"least {Rfq.MINIMUM_VENDORS}. Record a reason if fewer are capable of supplying."
        )
    rfq.status = Rfq.ISSUED
    rfq.issued_at = timezone.now()
    rfq.vendors.update(sent_at=timezone.now())
    rfq.save(update_fields=["status", "issued_at"])
    return rfq


@transaction.atomic
def record_quote(*, rfq_vendor: RfqVendor, quotes: list[dict], actor) -> RfqVendor:
    for quote in quotes:
        RfqQuoteLine.objects.update_or_create(
            rfq_vendor=rfq_vendor,
            request_line=quote["request_line"],
            defaults={
                "quoted_rate": Decimal(str(quote["rate"])),
                "quoted_qty": Decimal(str(quote.get("qty", quote["request_line"].quantity))),
                "delivery_date": quote.get("delivery_date"),
                "terms": quote.get("terms", ""),
            },
        )
    rfq_vendor.responded_at = timezone.now()
    rfq_vendor.save(update_fields=["responded_at"])
    rfq_vendor.rfq.status = Rfq.COMPARING
    rfq_vendor.rfq.save(update_fields=["status"])
    return rfq_vendor


def comparison(rfq: Rfq) -> list[dict]:
    """The comparison statement.

    Best price and earliest delivery are marked **as information only**. Nothing
    here selects a winner — that is the Purchase Manager's call alone.
    """
    statement = []
    for line in rfq.request.lines.all():
        quotes = [
            {
                "vendor": q.rfq_vendor.vendor,
                "rate": q.quoted_rate,
                "qty": q.quoted_qty,
                "amount": q.amount,
                "delivery_date": q.delivery_date,
                "terms": q.terms,
            }
            for q in RfqQuoteLine.objects.filter(
                rfq_vendor__rfq=rfq, request_line=line
            ).select_related("rfq_vendor__vendor")
        ]
        best_rate = min((q["rate"] for q in quotes), default=None)
        best_date = min((q["delivery_date"] for q in quotes if q["delivery_date"]), default=None)
        for quote in quotes:
            quote["is_best_price"] = best_rate is not None and quote["rate"] == best_rate
            quote["is_best_delivery"] = (
                best_date is not None and quote["delivery_date"] == best_date
            )
        statement.append(
            {
                "line": line,
                "quotes": quotes,
                "best_rate": best_rate,
                "awarded": Award.objects.filter(rfq=rfq, request_line=line).first(),
            }
        )
    return statement


@transaction.atomic
def award_line(*, rfq: Rfq, request_line, vendor: Vendor, actor, notes: str = "") -> Award:
    """Award a line to any vendor, irrespective of price.

    The frozen comparison snapshot is the audit trail. No justification is
    demanded for awarding above the lowest quote — that was stated as the
    Purchase Manager's prerogative and the platform honours it.
    """
    if not actor.has_capability("procurement:award"):
        raise DomainError(f"{actor} cannot award a purchase.")
    if not rfq.meets_minimum:
        raise DomainError(
            f"{rfq.number} has {rfq.responded_count} response(s). At least "
            f"{Rfq.MINIMUM_VENDORS} are required, or a recorded reason why fewer are possible."
        )

    quote = RfqQuoteLine.objects.filter(
        rfq_vendor__rfq=rfq, rfq_vendor__vendor=vendor, request_line=request_line
    ).first()
    if quote is None:
        raise DomainError(f"{vendor.name} did not quote this line.")

    snapshot = {
        "rfq": rfq.number,
        "line": request_line.description,
        "quotes": [
            {"vendor": q.rfq_vendor.vendor.name, "rate": str(q.quoted_rate),
             "delivery": str(q.delivery_date or "")}
            for q in RfqQuoteLine.objects.filter(
                rfq_vendor__rfq=rfq, request_line=request_line
            ).select_related("rfq_vendor__vendor")
        ],
    }
    award = Award.objects.create(
        rfq=rfq, request_line=request_line, winning_vendor=vendor,
        awarded_rate=quote.quoted_rate, awarded_qty=quote.quoted_qty,
        awarded_by=actor, comparison_snapshot=snapshot, notes=notes,
    )
    if not rfq.awards.count() < rfq.request.lines.count():
        rfq.status = Rfq.AWARDED
        rfq.save(update_fields=["status"])
    return award


# ---------------------------------------------------------- purchase orders
@transaction.atomic
def create_purchase_order(*, awards: list[Award], actor, expected_delivery=None) -> PurchaseOrder:
    """Raise an order from awarded lines, checked against the BOQ ceiling.

    The ceiling is consumed here rather than at confirmation: a draft order that
    could never be confirmed is worse than one refused up front.
    """
    if not awards:
        raise DomainError("Nothing awarded to order.")
    vendors = {a.winning_vendor_id for a in awards}
    if len(vendors) > 1:
        raise DomainError("One purchase order per vendor.")

    request = awards[0].rfq.request
    order = PurchaseOrder.objects.create(
        vendor=awards[0].winning_vendor,
        project=request.project,
        request=request,
        number=_number("PO", PurchaseOrder),
        expected_delivery=expected_delivery,
    )
    for award in awards:
        line = PurchaseOrderLine.objects.create(
            purchase_order=order,
            boq_line=award.request_line.boq_line,
            lot=award.request_line.boq_line.lot if award.request_line.boq_line else None,
            item=award.request_line.item,
            description=award.request_line.description,
            quantity=award.awarded_qty,
            uom=award.request_line.uom,
            rate=award.awarded_rate,
        )
        if line.boq_line_id:
            reserve_headroom(
                boq_line_id=line.boq_line_id,
                qty=line.quantity,
                document_type="purchase_order",
                document_id=order.pk,
                actor=actor,
                reason=f"{order.number} to {order.vendor.name}",
            )
    return order


DEFAULT_APPROVAL_THRESHOLD = Decimal("500000")


@transaction.atomic
def submit_purchase_order(
    *, order: PurchaseOrder, actor, threshold: Decimal | None = None
) -> PurchaseOrder:
    """Submit an order. Above the threshold it parks for the Purchase Manager.

    Deliberately does **not** raise to report that approval is needed. An
    earlier version set the status and then raised — and because the raise
    rolled the transaction back, the order never actually reached
    "awaiting approval". Needing a second signature is a normal outcome of
    submitting, not an error, so it is returned as a state.
    """
    if order.status == PurchaseOrder.CONFIRMED:
        raise DomainError(f"{order.number} is already confirmed.")
    if not order.lines.exists():
        raise DomainError(f"{order.number} has no lines.")

    threshold = threshold if threshold is not None else DEFAULT_APPROVAL_THRESHOLD
    if order.total_value > threshold and not actor.has_capability("purchase_order:approve"):
        order.status = PurchaseOrder.AWAITING_APPROVAL
        order.save(update_fields=["status"])
        return order

    return _confirm(order=order, actor=actor)


@transaction.atomic
def approve_purchase_order(*, order: PurchaseOrder, actor) -> PurchaseOrder:
    """The Purchase Manager's second signature above the threshold."""
    if not actor.has_capability("purchase_order:approve"):
        raise DomainError(f"{actor} cannot approve a purchase order.")
    if order.status != PurchaseOrder.AWAITING_APPROVAL:
        raise DomainError(
            f"{order.number} is {order.get_status_display()} — nothing to approve."
        )
    return _confirm(order=order, actor=actor)


def _confirm(*, order: PurchaseOrder, actor) -> PurchaseOrder:
    order.status = PurchaseOrder.CONFIRMED
    order.confirmed_at = timezone.now()
    order.save(update_fields=["status", "confirmed_at"])
    order.lock(actor)

    events.emit(
        "PurchaseOrderConfirmed",
        {"order_id": order.pk},
        idempotency_key=f"PurchaseOrderConfirmed:{order.pk}",
    )
    return order


@transaction.atomic
def amend_line(*, line: PurchaseOrderLine, new_qty: Decimal, reason: str, actor) -> PurchaseOrderLine:
    """Reduce an outstanding quantity, releasing the ceiling it held."""
    new_qty = Decimal(str(new_qty))
    if new_qty >= line.quantity:
        raise DomainError("An amendment reduces a quantity. Raise a new order to increase one.")
    if new_qty < line.received_qty:
        raise DomainError(
            f"{line.received_qty} has already been received; the order cannot be "
            f"reduced below that. Raise a return instead."
        )

    from .models import PoAmendment

    released = line.quantity - new_qty
    PoAmendment.objects.create(
        line=line, previous_qty=line.quantity, new_qty=new_qty, reason=reason, actor=actor
    )
    if line.boq_line_id:
        release_headroom(
            boq_line_id=line.boq_line_id,
            qty=released,
            document_type="purchase_order",
            document_id=line.purchase_order_id,
            actor=actor,
            reason=f"Amended {line.purchase_order.number}: {reason}",
        )
    line.quantity = new_qty
    line.save(update_fields=["quantity"])
    return line
