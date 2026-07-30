"""Receipt, verification, and the point where cost enters a project.

Process design §4.9. Nothing enters a project's cost on the strength of an
unverified delivery — the Store Keeper records what arrived, the Site Engineer
verifies it, and only then does stock post and cost land.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.models import Location
from apps.platform_core.exceptions import DomainError
from apps.platform_core.models import CostEntry
from apps.platform_core.services import costing, events
from apps.platform_core.services.ceiling import release_headroom
from apps.platform_core.services.stock import post_move
from apps.procurement.models import PurchaseOrder, PurchaseOrderLine

from .models import Discrepancy, ExpectedReceipt, GoodsReceipt, MaterialReturn, ReceiptVerification

ZERO = Decimal("0")


def _number(prefix: str, model) -> str:
    stamp = timezone.now().strftime("%y%m%d")
    count = model.objects.filter(number__startswith=f"{prefix}-{stamp}").count()
    return f"{prefix}-{stamp}-{count + 1:03d}"


@events.handles("PurchaseOrderConfirmed")
def _expect_receipts(payload: dict) -> None:
    """A confirmed order creates the expected receipt — SUPPLY lines only.

    A service line has nothing to receive, so no receipt is created for it and
    the Store Keeper is never asked to "receive" a labour line.
    """
    order = PurchaseOrder.objects.get(pk=payload["order_id"])
    if ExpectedReceipt.objects.filter(purchase_order_line__purchase_order=order).exists():
        return  # at-least-once delivery

    site = Location.objects.filter(project=order.project, kind=Location.SITE).first()
    if site is None:
        site = Location.objects.create(
            code=f"{order.project.code}-SITE", name=f"{order.project.name} site",
            kind=Location.SITE, project=order.project,
        )
    for line in order.lines.select_related("boq_line"):
        if line.boq_line and line.boq_line.route == line.boq_line.SERVICE:
            continue
        ExpectedReceipt.objects.create(
            purchase_order_line=line, location=site,
            expected_qty=line.quantity, expected_date=order.expected_delivery,
        )


@transaction.atomic
def record_receipt(
    *, purchase_order_line: PurchaseOrderLine, quantity, actor,
    location=None, vendor_challan: str = "", received_at=None,
) -> GoodsReceipt:
    """The Store Keeper records what physically arrived. No cost yet."""
    quantity = Decimal(str(quantity))
    if quantity <= ZERO:
        raise DomainError("A receipt needs a positive quantity.")
    if purchase_order_line.purchase_order.status != PurchaseOrder.CONFIRMED:
        raise DomainError("Only a confirmed purchase order can be received against.")

    outstanding = purchase_order_line.outstanding_qty
    if quantity > outstanding:
        raise DomainError(
            f"{outstanding} outstanding on this line, but {quantity} was recorded. "
            f"Amend the order first if the vendor genuinely sent more."
        )

    expected = ExpectedReceipt.objects.filter(
        purchase_order_line=purchase_order_line
    ).first()
    location = location or (expected.location if expected else None)
    if location is None:
        raise DomainError("No site location to receive into.")

    receipt = GoodsReceipt.objects.create(
        expected_receipt=expected,
        purchase_order_line=purchase_order_line,
        location=location,
        number=_number("GRN", GoodsReceipt),
        received_qty=quantity,
        received_by=actor,
        received_at=received_at or timezone.now(),
        vendor_challan=vendor_challan,
    )
    return receipt


@transaction.atomic
def verify_receipt(
    *, receipt: GoodsReceipt, accepted_qty, actor,
    rejected_qty=ZERO, notes: str = "", photographs=None,
) -> ReceiptVerification:
    """The Site Engineer's check — and the moment cost enters the project.

    A mismatch logs a discrepancy that holds the vendor bill, and releases the
    rejected quantity's headroom so a replacement can be ordered.
    """
    if not actor.has_capability("receipt:verify"):
        raise DomainError(f"{actor} cannot verify a goods receipt.")
    if hasattr(receipt, "verification"):
        raise DomainError(f"{receipt.number} has already been verified.")

    accepted_qty = Decimal(str(accepted_qty))
    rejected_qty = Decimal(str(rejected_qty))
    if accepted_qty + rejected_qty > receipt.received_qty:
        raise DomainError(
            f"Accepted plus rejected ({accepted_qty + rejected_qty}) exceeds the "
            f"{receipt.received_qty} recorded as received."
        )

    verification = ReceiptVerification.objects.create(
        goods_receipt=receipt, verified_by=actor, verified_at=timezone.now(),
        accepted_qty=accepted_qty, rejected_qty=rejected_qty,
        discrepancy_notes=notes, photographs=photographs or [],
    )

    line = receipt.purchase_order_line
    if accepted_qty > ZERO:
        post_move(
            item_id=line.item_id,
            boq_line_id=line.boq_line_id,
            quantity=accepted_qty,
            to_location_id=receipt.location_id,
            unit_value=line.rate,
            source_type="goods_receipt",
            source_id=receipt.pk,
            actor=actor,
        )
        costing.post_cost(
            project_id=receipt.location.project_id or line.purchase_order.project_id,
            lot_id=line.lot_id,
            boq_line_id=line.boq_line_id,
            category=CostEntry.MATERIAL,
            amount=accepted_qty * line.rate,
            source_type="goods_receipt",
            source_id=receipt.pk,
            actor=actor,
        )

    if rejected_qty > ZERO:
        Discrepancy.objects.create(
            verification=verification, quantity=rejected_qty,
            reason=notes or "Quantity or quality did not match the order.",
        )
        # Free the ceiling the rejected quantity held, so a replacement can be
        # ordered without a BOQ revision.
        if line.boq_line_id:
            release_headroom(
                boq_line_id=line.boq_line_id, qty=rejected_qty,
                document_type="purchase_order", document_id=line.purchase_order_id,
                actor=actor, reason=f"Rejected on {receipt.number}",
            )
        receipt.status = GoodsReceipt.DISCREPANCY
    else:
        receipt.status = GoodsReceipt.VERIFIED
    receipt.save(update_fields=["status"])

    if receipt.expected_receipt_id:
        expected = receipt.expected_receipt
        expected.status = (
            ExpectedReceipt.COMPLETE
            if line.received_qty >= line.quantity
            else ExpectedReceipt.PARTIAL
        )
        expected.save(update_fields=["status"])

    events.emit("ReceiptVerified", {"receipt_id": receipt.pk})
    return verification


@transaction.atomic
def return_material(
    *, purchase_order_line: PurchaseOrderLine, quantity, reason: str, actor,
    goods_receipt=None, debit_note_number: str = "",
) -> MaterialReturn:
    """Send material back, releasing its commitment and reversing its cost.

    This is the path that makes the ceiling's arithmetic hold: what goes back
    frees headroom, so the same material can be re-ordered.
    """
    quantity = Decimal(str(quantity))
    if quantity <= ZERO:
        raise DomainError("A return needs a positive quantity.")
    if quantity > purchase_order_line.received_qty:
        raise DomainError(
            f"Only {purchase_order_line.received_qty} has been received; "
            f"{quantity} cannot be returned."
        )

    location = (
        goods_receipt.location
        if goods_receipt
        else Location.objects.filter(project=purchase_order_line.purchase_order.project).first()
    )
    material_return = MaterialReturn.objects.create(
        goods_receipt=goods_receipt,
        purchase_order_line=purchase_order_line,
        number=_number("RET", MaterialReturn),
        quantity=quantity, reason=reason, actor=actor,
        debit_note_number=debit_note_number,
    )

    post_move(
        item_id=purchase_order_line.item_id,
        boq_line_id=purchase_order_line.boq_line_id,
        quantity=quantity,
        from_location_id=location.pk if location else None,
        unit_value=purchase_order_line.rate,
        source_type="material_return",
        source_id=material_return.pk,
        actor=actor,
    )
    costing.post_cost(
        project_id=purchase_order_line.purchase_order.project_id,
        lot_id=purchase_order_line.lot_id,
        boq_line_id=purchase_order_line.boq_line_id,
        category=CostEntry.MATERIAL,
        amount=-(quantity * purchase_order_line.rate),
        source_type="material_return",
        source_id=material_return.pk,
        actor=actor,
    )
    if purchase_order_line.boq_line_id:
        release_headroom(
            boq_line_id=purchase_order_line.boq_line_id,
            qty=quantity,
            document_type="purchase_order",
            document_id=purchase_order_line.purchase_order_id,
            actor=actor,
            reason=f"Returned on {material_return.number}: {reason}",
        )
    events.emit("MaterialReturned", {"return_id": material_return.pk})
    return material_return
