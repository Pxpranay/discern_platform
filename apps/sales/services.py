"""Sales workflow: confirm an order, then gate its kickoff.

Process design §4.2. A confirmed order does **not** create a project. Someone
marks it approved for kickoff first; until then it sits in "held pending
review" and is re-checked, rather than silently proceeding.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.platform_core.exceptions import DomainError
from apps.platform_core.models import CostEntry
from apps.platform_core.services import costing, events

from .models import ChangeOrder, ClientInvoice, Lot, Order, Quotation, QuotationLot


@transaction.atomic
def confirm_order(*, order: Order, committed_delivery_date, actor) -> Order:
    """Confirm an order, fixing price per lot and the committed delivery date."""
    if order.status not in (Order.DRAFT, Order.HELD):
        raise DomainError(f"Order {order.number} is {order.status} and cannot be confirmed.")
    if not order.lots.exists():
        raise DomainError(f"Order {order.number} has no lots; nothing to confirm.")
    if committed_delivery_date is None:
        raise DomainError(
            "A confirmed order must carry a committed delivery date — it is the "
            "ceiling the project schedule is planned against."
        )

    order.status = Order.HELD
    order.confirmed_at = timezone.now()
    order.committed_delivery_date = committed_delivery_date
    order.save()

    events.emit(
        "OrderConfirmed",
        {"order_id": order.pk, "value": str(order.total_value)},
        idempotency_key=f"OrderConfirmed:{order.pk}",
    )
    return order


@transaction.atomic
def approve_for_kickoff(*, order: Order, actor) -> Order:
    """The gate between Sales and Project.

    Emitting the event is what creates the project — see
    ``apps.projects.services.initiate_project``, which handles it.
    """
    if order.status != Order.HELD:
        raise DomainError(
            f"Order {order.number} is {order.status}; only a confirmed order "
            f"awaiting review can be approved for kickoff."
        )
    if not actor.has_capability("order:approve_kickoff"):
        raise DomainError(f"{actor} cannot approve an order for kickoff.")

    order.status = Order.KICKED_OFF
    order.kickoff_approved_by = actor
    order.kickoff_approved_at = timezone.now()
    order.save()

    events.emit(
        "OrderApprovedForKickoff",
        {"order_id": order.pk, "actor_id": actor.pk},
        idempotency_key=f"OrderApprovedForKickoff:{order.pk}",
    )
    return order


@transaction.atomic
def hold_for_review(*, order: Order, reason: str, actor) -> Order:
    """A 'no' at the kickoff gate parks the order rather than losing it."""
    order.status = Order.HELD
    order.save()
    return order


@transaction.atomic
def apply_change_order(*, change_order: ChangeOrder, actor) -> Order:
    """The only mechanism that alters a confirmed order.

    Adjusts a lot's price and/or the committed delivery date. Deliberately does
    not touch the BOQ — detail flows one way only.
    """
    order = change_order.order
    if change_order.lot and change_order.price_delta:
        lot = change_order.lot
        lot.price = lot.price + change_order.price_delta
        lot.save(update_fields=["price"])

    if change_order.new_committed_date:
        order.committed_delivery_date = change_order.new_committed_date
        order.save(update_fields=["committed_delivery_date"])
        events.emit(
            "OrderCommittedDateChanged",
            {
                "order_id": order.pk,
                "new_date": str(change_order.new_committed_date),
            },
        )

    change_order.lock(actor)
    return order


@transaction.atomic
def issue_invoice(*, invoice: ClientInvoice, actor) -> ClientInvoice:
    """Issue a client invoice and post REVENUE against its lot."""
    if invoice.status != ClientInvoice.DRAFT:
        raise DomainError(f"Invoice {invoice.number} is already {invoice.status}.")

    project = invoice.lot.project
    if project is None:
        raise DomainError(
            f"Lot {invoice.lot} has no project yet; revenue must land on a project."
        )

    invoice.status = ClientInvoice.ISSUED
    invoice.issued_at = timezone.now()
    invoice.save(update_fields=["status", "issued_at"])

    costing.post_cost(
        project_id=project.pk,
        lot_id=invoice.lot.pk,
        category=CostEntry.REVENUE,
        amount=invoice.amount,
        source_type="client_invoice",
        source_id=invoice.pk,
        actor=actor,
        effective_date=invoice.invoice_date,
    )
    events.emit("InvoiceIssued", {"invoice_id": invoice.pk})
    return invoice


@transaction.atomic
def copy_quotation_lots_to_order(*, quotation: Quotation, order: Order) -> list[Lot]:
    """Carry the accepted quotation's lots onto the order without re-typing."""
    created = []
    for qlot in quotation.lots.all():
        created.append(
            Lot.objects.create(
                order=order,
                name=qlot.name,
                kind=qlot.kind,
                price=qlot.price,
                scope_description=qlot.scope_description,
                sequence=qlot.sequence,
            )
        )
    return created
