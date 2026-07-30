"""Receipt, verification and returns.

Process design §4.9. The Store Keeper records what arrived; a **Site Engineer
verifies it** before anything enters the project's cost. Those are two records
by two roles on purpose — collapsing them into flags on one row is how
"verification" quietly becomes a checkbox the receiving user ticks themselves.
"""

from decimal import Decimal

from django.db import models


class ExpectedReceipt(models.Model):
    """Created automatically when a purchase order is confirmed — SUPPLY lines
    only. A service line has nothing to receive."""

    PENDING = "pending"
    PARTIAL = "partial"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (PENDING, "Awaited"),
        (PARTIAL, "Partly received"),
        (COMPLETE, "Received"),
        (CANCELLED, "Cancelled"),
    ]

    purchase_order_line = models.ForeignKey(
        "procurement.PurchaseOrderLine", on_delete=models.CASCADE, related_name="expected_receipts"
    )
    location = models.ForeignKey(
        "core.Location", on_delete=models.PROTECT, related_name="expected_receipts"
    )
    expected_qty = models.DecimalField(max_digits=18, decimal_places=4)
    expected_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "expected_receipt"
        ordering = ["expected_date", "id"]

    def __str__(self) -> str:
        return f"Expected {self.expected_qty} at {self.location.code}"


class GoodsReceipt(models.Model):
    """What the Store Keeper says arrived. Not yet cost."""

    RECORDED = "recorded"
    VERIFIED = "verified"
    DISCREPANCY = "discrepancy"
    STATUS_CHOICES = [
        (RECORDED, "Recorded, awaiting verification"),
        (VERIFIED, "Verified"),
        (DISCREPANCY, "Discrepancy logged"),
    ]

    expected_receipt = models.ForeignKey(
        ExpectedReceipt, on_delete=models.PROTECT, null=True, blank=True, related_name="receipts"
    )
    purchase_order_line = models.ForeignKey(
        "procurement.PurchaseOrderLine", on_delete=models.PROTECT, related_name="receipts"
    )
    location = models.ForeignKey(
        "core.Location", on_delete=models.PROTECT, related_name="receipts"
    )
    number = models.CharField(max_length=64, unique=True)
    received_qty = models.DecimalField(max_digits=18, decimal_places=4)
    received_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="receipts_recorded"
    )
    received_at = models.DateTimeField()
    vendor_challan = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=RECORDED)

    class Meta:
        db_table = "goods_receipt"
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return self.number


class ReceiptVerification(models.Model):
    """The Site Engineer's check. **Cost posts on this, not on receipt.**

    Nothing enters a project's books on the strength of an unverified delivery.
    """

    goods_receipt = models.OneToOneField(
        GoodsReceipt, on_delete=models.PROTECT, related_name="verification"
    )
    verified_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="verifications"
    )
    verified_at = models.DateTimeField()
    accepted_qty = models.DecimalField(max_digits=18, decimal_places=4)
    rejected_qty = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal("0"))
    discrepancy_notes = models.TextField(blank=True)
    photographs = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "receipt_verification"

    @property
    def has_discrepancy(self) -> bool:
        return self.rejected_qty > 0


class Discrepancy(models.Model):
    """A shortfall or rejection, and what is being done about it.

    Holds the vendor bill until resolved — cost must not enter a project on an
    unverified delivery, and releasing first to correct later is how bad numbers
    become permanent.
    """

    OPEN = "open"
    DEBIT_NOTE = "debit_note_raised"
    REPLACEMENT = "replacement_requested"
    CLOSED = "closed"
    STATUS_CHOICES = [
        (OPEN, "Open"),
        (DEBIT_NOTE, "Debit note raised"),
        (REPLACEMENT, "Replacement requested"),
        (CLOSED, "Closed"),
    ]

    verification = models.ForeignKey(
        ReceiptVerification, on_delete=models.CASCADE, related_name="discrepancies"
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    reason = models.TextField()
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=OPEN)
    debit_note_number = models.CharField(max_length=64, blank=True)
    holds_vendor_bill = models.BooleanField(default=True)
    raised_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "discrepancy"


class MaterialReturn(models.Model):
    """Material going back to the vendor.

    Releases the BOQ commitment and reverses the cost, so the headroom the
    return frees can be re-ordered — the defect the commitment ledger exists
    to fix.
    """

    DRAFT = "draft"
    SENT = "sent"
    CREDITED = "credited"
    STATUS_CHOICES = [(DRAFT, "Draft"), (SENT, "Sent to vendor"), (CREDITED, "Credit received")]

    goods_receipt = models.ForeignKey(
        GoodsReceipt, on_delete=models.PROTECT, null=True, blank=True, related_name="returns"
    )
    purchase_order_line = models.ForeignKey(
        "procurement.PurchaseOrderLine", on_delete=models.PROTECT, related_name="returns"
    )
    number = models.CharField(max_length=64, unique=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    reason = models.TextField()
    debit_note_number = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=DRAFT)
    actor = models.ForeignKey("accounts.AppUser", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "material_return"
        ordering = ["-created_at"]
