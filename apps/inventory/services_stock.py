"""Dead/excess stock flagging and inter-project redeployment."""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.platform_core.exceptions import DomainError
from apps.platform_core.models import CostEntry, Notification
from apps.platform_core.services import costing, events
from apps.platform_core.services.stock import on_hand, post_move, valuation_at

from .models import ExcessStockFlag, StockTransfer

ZERO = Decimal("0")


def _number(prefix, model):
    stamp = timezone.now().strftime("%y%m%d")
    n = model.objects.filter(number__startswith=f"{prefix}-{stamp}").count()
    return f"{prefix}-{stamp}-{n + 1:03d}"


@transaction.atomic
def flag_excess(*, item, location, quantity, actor, reason=ExcessStockFlag.AVAILABLE,
                goods_receipt=None, notes="") -> ExcessStockFlag:
    """A Site Engineer marks stock no longer needed by their project.

    Not a scrap. Scrapping writes stock off the books; this relabels it as
    usable elsewhere, which is the opposite.
    """
    if not actor.has_capability("stock:flag_excess"):
        raise DomainError(f"{actor} cannot flag excess stock.")

    quantity = Decimal(str(quantity))
    available = on_hand(item.pk, location.pk)
    if quantity > available:
        raise DomainError(f"Only {available} of {item} is at {location.code}.")

    unit_value = (
        goods_receipt.purchase_order_line.rate
        if goods_receipt is not None
        else valuation_at(item.pk, location.pk)
    )

    flag = ExcessStockFlag.objects.create(
        goods_receipt=goods_receipt, item=item, project=location.project,
        location=location, quantity=quantity, unit_value=unit_value,
        reason=reason, notes=notes, flagged_by=actor,
    )
    events.emit("StockFlaggedExcess", {"flag_id": flag.pk})
    return flag


@events.handles("StockFlaggedExcess")
def _fan_out(payload: dict) -> None:
    """Straight to three dashboards, so whoever can act sees it the same day
    rather than at a routine review."""
    from apps.accounts.models import AppUser

    flag = ExcessStockFlag.objects.select_related("item", "project", "location").get(
        pk=payload["flag_id"]
    )
    recipients = AppUser.objects.filter(
        user_roles__role__capabilities__contains=["stock:transfer"]
    ).distinct()
    if flag.project and flag.project.project_manager:
        recipients = list(recipients) + [flag.project.project_manager]

    for user in set(recipients):
        Notification.objects.create(
            user=user, event_name="StockFlaggedExcess",
            title=f"{flag.quantity} {flag.item.uom} of {flag.item.name} available",
            body=f"Flagged at {flag.location.code} on {flag.project.code}: "
                 f"{flag.get_reason_display()}",
            entity_type="ExcessStockFlag", entity_id=flag.pk,
        )


@transaction.atomic
def redeploy(*, flag: ExcessStockFlag, to_location, actor, quantity=None, reason="") -> StockTransfer:
    """Propose moving flagged stock to a project that needs it.

    The receiving Project Manager must accept: cost is about to land on their
    books, so its owner gets a say.
    """
    if not actor.has_capability("stock:transfer"):
        raise DomainError(f"{actor} cannot redeploy stock.")

    quantity = Decimal(str(quantity)) if quantity is not None else flag.quantity
    if quantity > flag.quantity:
        raise DomainError(f"Only {flag.quantity} was flagged.")

    return StockTransfer.objects.create(
        excess_flag=flag, item=flag.item, from_location=flag.location,
        to_location=to_location, quantity=quantity, unit_value=flag.unit_value,
        reason=reason, number=_number("TRF", StockTransfer), requested_by=actor,
    )


@transaction.atomic
def accept_transfer(*, transfer: StockTransfer, actor) -> StockTransfer:
    """The receiving side accepts, and the paired cost entries post.

    This is the one deliberate breach of project isolation, which is exactly
    why the value moves explicitly rather than the stock moving silently.
    """
    if transfer.status != StockTransfer.PENDING:
        raise DomainError(f"{transfer.number} is {transfer.get_status_display()}.")

    post_move(
        item_id=transfer.item_id, quantity=transfer.quantity,
        from_location_id=transfer.from_location_id, to_location_id=transfer.to_location_id,
        unit_value=transfer.unit_value, source_type="stock_transfer",
        source_id=transfer.pk, actor=actor,
    )

    value = (transfer.unit_value or ZERO) * transfer.quantity
    releasing = transfer.from_location.project
    receiving = transfer.to_location.project
    if value > ZERO and releasing and receiving:
        costing.post_cost(
            project_id=releasing.pk, category=CostEntry.STOCK_OUT, amount=-value,
            source_type="stock_transfer", source_id=transfer.pk, actor=actor,
        )
        costing.post_cost(
            project_id=receiving.pk, category=CostEntry.STOCK_IN, amount=value,
            source_type="stock_transfer", source_id=transfer.pk, actor=actor,
        )

    transfer.status = StockTransfer.COMPLETE
    transfer.accepted_by = actor
    transfer.completed_at = timezone.now()
    transfer.save(update_fields=["status", "accepted_by", "completed_at"])

    if transfer.excess_flag_id:
        flag = transfer.excess_flag
        flag.status = ExcessStockFlag.TRANSFERRED
        flag.resolved_at = timezone.now()
        flag.save(update_fields=["status", "resolved_at"])

    events.emit("StockTransferred", {"transfer_id": transfer.pk})
    return transfer


@transaction.atomic
def decline_transfer(*, transfer: StockTransfer, actor, reason="") -> StockTransfer:
    transfer.status = StockTransfer.DECLINED
    transfer.reason = f"{transfer.reason}\nDeclined: {reason}".strip()
    transfer.save(update_fields=["status", "reason"])
    return transfer
