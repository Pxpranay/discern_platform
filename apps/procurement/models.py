"""Vendors, procurement requests, RFQs, awards and purchase orders.

Process design §4.6–§4.7. Three ways a purchase need arises, one record; at
least three vendors quoted; a comparison statement; and an award that is
entirely the Purchase Manager's own call, irrespective of price.
"""

from decimal import Decimal

from django.db import models
from django.db.models import Sum

from apps.platform_core.models import Approvable


class Vendor(models.Model):
    name = models.CharField(max_length=256)
    gstin = models.CharField(max_length=32, blank=True)
    address = models.TextField(blank=True)
    contact_name = models.CharField(max_length=128, blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    contact_email = models.EmailField(blank=True)
    trades = models.JSONField(default=list, blank=True)

    #: Empanelled vendors with an agreed rate can be issued a service order
    #: directly, skipping the RFQ round (process design §4.10).
    is_empanelled = models.BooleanField(default=False)
    payment_terms = models.CharField(max_length=128, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "vendor"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class VendorRate(models.Model):
    """An agreed rate. What lets a service order skip price discovery."""

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="rates")
    item = models.ForeignKey(
        "core.Item", on_delete=models.CASCADE, null=True, blank=True, related_name="vendor_rates"
    )
    description = models.CharField(max_length=256, blank=True)
    rate = models.DecimalField(max_digits=18, decimal_places=2)
    uom = models.CharField(max_length=32, default="nos")
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "vendor_rate"


class RequestSource(models.TextChoices):
    BOQ_RELEASE = "BOQ_RELEASE", "BOQ revision released"
    SITE_REQUISITION = "SITE_REQUISITION", "Raised on site"
    FABRICATION_SHORTFALL = "FABRICATION_SHORTFALL", "Fabrication raw material"


class ProcurementRequest(models.Model):
    """One record for all three sources (process design §4.6).

    The ``source`` field is the only difference, so everything downstream is
    written once. A site requisition additionally needs the Project Manager's
    approval before Procurement may act on it at all.
    """

    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    HELD = "held"
    SOURCED = "sourced"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (AWAITING_APPROVAL, "Awaiting Project Manager approval"),
        (APPROVED, "Approved"),
        (HELD, "Held for re-review"),
        (SOURCED, "Sourced"),
        (CANCELLED, "Cancelled"),
    ]

    project = models.ForeignKey(
        "core.Project", on_delete=models.PROTECT, related_name="procurement_requests"
    )
    source = models.CharField(max_length=32, choices=RequestSource.choices)
    boq_revision = models.ForeignKey(
        "engineering.BoqRevision", on_delete=models.PROTECT, null=True, blank=True,
        related_name="procurement_requests",
    )
    number = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=DRAFT)

    #: Kept visible throughout Procurement: buyers should know a request came
    #: from an on-site call rather than a scheduled BOQ cycle.
    is_site_raised = models.BooleanField(default=False)

    requested_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="procurement_requests"
    )
    approved_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    hold_reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "procurement_request"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.number

    @property
    def needs_approval(self) -> bool:
        return self.source == RequestSource.SITE_REQUISITION


class ProcurementRequestLine(models.Model):
    request = models.ForeignKey(
        ProcurementRequest, on_delete=models.CASCADE, related_name="lines"
    )
    boq_line = models.ForeignKey(
        "core.BoqLine", on_delete=models.PROTECT, null=True, blank=True,
        related_name="procurement_lines",
    )
    item = models.ForeignKey(
        "core.Item", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    description = models.TextField()
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    uom = models.CharField(max_length=32, default="nos")
    required_by = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "procurement_request_line"


class Rfq(models.Model):
    """A request for quotation, sent to several vendors for the same lines."""

    DRAFT = "draft"
    ISSUED = "issued"
    COMPARING = "comparing"
    AWARDED = "awarded"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (ISSUED, "Issued to vendors"),
        (COMPARING, "Quotes in, comparing"),
        (AWARDED, "Awarded"),
        (CANCELLED, "Cancelled"),
    ]

    #: Discern's rule: every line quoted by more than two vendors.
    MINIMUM_VENDORS = 3

    request = models.ForeignKey(
        ProcurementRequest, on_delete=models.PROTECT, related_name="rfqs"
    )
    number = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=DRAFT)
    issued_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateField(null=True, blank=True)

    #: A specialised item with only one or two capable suppliers is a real
    #: situation. Blocking it indefinitely would be worse than recording why.
    min_vendors_waived_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rfq"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.number

    @property
    def responded_count(self) -> int:
        return self.vendors.filter(responded_at__isnull=False).count()

    @property
    def meets_minimum(self) -> bool:
        return self.responded_count >= self.MINIMUM_VENDORS or bool(
            self.min_vendors_waived_reason.strip()
        )


class RfqVendor(models.Model):
    rfq = models.ForeignKey(Rfq, on_delete=models.CASCADE, related_name="vendors")
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="rfq_invitations")
    sent_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "rfq_vendor"
        constraints = [
            models.UniqueConstraint(fields=["rfq", "vendor"], name="uniq_rfq_vendor")
        ]

    def __str__(self) -> str:
        return f"{self.rfq.number} · {self.vendor.name}"


class RfqQuoteLine(models.Model):
    rfq_vendor = models.ForeignKey(
        RfqVendor, on_delete=models.CASCADE, related_name="quote_lines"
    )
    request_line = models.ForeignKey(
        ProcurementRequestLine, on_delete=models.CASCADE, related_name="quotes"
    )
    quoted_rate = models.DecimalField(max_digits=18, decimal_places=2)
    quoted_qty = models.DecimalField(max_digits=18, decimal_places=4)
    delivery_date = models.DateField(null=True, blank=True)
    terms = models.CharField(max_length=256, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "rfq_quote_line"
        constraints = [
            models.UniqueConstraint(
                fields=["rfq_vendor", "request_line"], name="uniq_quote_per_line"
            )
        ]

    @property
    def amount(self) -> Decimal:
        return self.quoted_rate * self.quoted_qty


class Award(models.Model):
    """The Purchase Manager's pick — entirely discretionary.

    Nothing in the platform auto-selects, defaults to lowest, or demands a
    justification for awarding elsewhere. ``comparison_snapshot`` freezes what
    was actually on screen at the moment of award, which is a stronger audit
    trail than a free-text reason nobody reads.
    """

    rfq = models.ForeignKey(Rfq, on_delete=models.PROTECT, related_name="awards")
    request_line = models.ForeignKey(
        ProcurementRequestLine, on_delete=models.PROTECT, related_name="awards"
    )
    winning_vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="awards")
    awarded_rate = models.DecimalField(max_digits=18, decimal_places=2)
    awarded_qty = models.DecimalField(max_digits=18, decimal_places=4)
    awarded_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="awards"
    )
    awarded_at = models.DateTimeField(auto_now_add=True)
    comparison_snapshot = models.JSONField(default=dict)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "award"
        constraints = [
            models.UniqueConstraint(
                fields=["rfq", "request_line"], name="uniq_award_per_line"
            )
        ]

    @property
    def was_lowest(self) -> bool | None:
        """Informational only. Never enforced."""
        rows = self.comparison_snapshot.get("quotes") or []
        if not rows:
            return None
        return self.awarded_rate <= min(Decimal(str(r["rate"])) for r in rows)


class PurchaseOrder(Approvable):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (AWAITING_APPROVAL, "Awaiting Purchase Manager approval"),
        (CONFIRMED, "Confirmed"),
        (CANCELLED, "Cancelled"),
    ]

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="purchase_orders")
    project = models.ForeignKey(
        "core.Project", on_delete=models.PROTECT, related_name="purchase_orders"
    )
    request = models.ForeignKey(
        ProcurementRequest, on_delete=models.PROTECT, null=True, blank=True,
        related_name="purchase_orders",
    )
    number = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=DRAFT)
    expected_delivery = models.DateField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    audit_fields = ("number", "status")

    class Meta:
        db_table = "purchase_order"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.number

    @property
    def total_value(self) -> Decimal:
        total = self.lines.aggregate(
            t=Sum(models.F("quantity") * models.F("rate"), output_field=models.DecimalField())
        )["t"]
        return total or Decimal("0")

    @property
    def approval_value(self) -> Decimal:
        return self.total_value


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="lines"
    )
    boq_line = models.ForeignKey(
        "core.BoqLine", on_delete=models.PROTECT, null=True, blank=True, related_name="po_lines"
    )
    lot = models.ForeignKey(
        "sales.Lot", on_delete=models.PROTECT, null=True, blank=True, related_name="po_lines"
    )
    item = models.ForeignKey(
        "core.Item", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    description = models.TextField()
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    uom = models.CharField(max_length=32, default="nos")
    rate = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        db_table = "purchase_order_line"

    @property
    def amount(self) -> Decimal:
        return self.quantity * self.rate

    @property
    def received_qty(self) -> Decimal:
        """Derived, never stored — the accepted quantity across verified receipts."""
        from apps.inventory.models import ReceiptVerification

        total = ReceiptVerification.objects.filter(
            goods_receipt__purchase_order_line=self
        ).aggregate(t=Sum("accepted_qty"))["t"]
        return total or Decimal("0")

    @property
    def outstanding_qty(self) -> Decimal:
        return self.quantity - self.received_qty


class PoAmendment(models.Model):
    """A change to a confirmed order's quantity, with the reason on the record."""

    line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.CASCADE, related_name="amendments"
    )
    previous_qty = models.DecimalField(max_digits=18, decimal_places=4)
    new_qty = models.DecimalField(max_digits=18, decimal_places=4)
    reason = models.TextField()
    actor = models.ForeignKey("accounts.AppUser", on_delete=models.PROTECT, related_name="+")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "po_amendment"
        ordering = ["-at"]
