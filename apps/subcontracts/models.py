"""Subcontract execution.

Process design §4.10. Subcontracted work does not go out to tender the way
material does — Discern has empanelled subcontractors on agreed rates, so a
service order goes direct from the BOQ line with no RFQ round. What a purchase
order screen cannot give, and this module does, is **progress against the BOQ
scope it was raised from**, and certification as a distinct act before billing.
"""

from decimal import Decimal

from django.db import models
from django.db.models import Sum

from apps.platform_core.models import Approvable


class ServiceOrder(Approvable):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    ISSUED = "issued"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (AWAITING_APPROVAL, "Awaiting Purchase Manager approval"),
        (ISSUED, "Issued to subcontractor"),
        (COMPLETE, "Complete"),
        (CANCELLED, "Cancelled"),
    ]

    project = models.ForeignKey(
        "core.Project", on_delete=models.PROTECT, related_name="service_orders"
    )
    lot = models.ForeignKey(
        "sales.Lot", on_delete=models.PROTECT, null=True, blank=True, related_name="service_orders"
    )
    boq_line = models.ForeignKey(
        "core.BoqLine", on_delete=models.PROTECT, related_name="service_orders"
    )
    vendor = models.ForeignKey(
        "procurement.Vendor", on_delete=models.PROTECT, related_name="service_orders"
    )
    number = models.CharField(max_length=64, unique=True)
    scope_description = models.TextField()
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    uom = models.CharField(max_length=32, default="nos")
    rate = models.DecimalField(max_digits=18, decimal_places=2)

    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=DRAFT)
    issued_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    audit_fields = ("number", "status", "quantity", "rate")

    class Meta:
        db_table = "service_order"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.number

    @property
    def total_value(self) -> Decimal:
        return self.quantity * self.rate

    @property
    def approval_value(self) -> Decimal:
        return self.total_value

    @property
    def certified_qty(self) -> Decimal:
        """Derived. Never stored."""
        total = self.certifications.aggregate(t=Sum("certified_quantity"))["t"]
        return total or Decimal("0")

    @property
    def outstanding_qty(self) -> Decimal:
        return self.quantity - self.certified_qty

    @property
    def percent_certified(self) -> Decimal:
        if not self.quantity:
            return Decimal("0")
        return (self.certified_qty / self.quantity) * Decimal("100")


class ServiceProgress(models.Model):
    """Day-to-day progress, logged by whoever can see the work.

    Site coordinators are best placed to report what has actually been done, so
    anyone with visibility may log — but logging is not certifying.
    """

    service_order = models.ForeignKey(
        ServiceOrder, on_delete=models.CASCADE, related_name="progress"
    )
    reported_at = models.DateTimeField(auto_now_add=True)
    reported_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="service_progress"
    )
    percent_complete = models.DecimalField(max_digits=5, decimal_places=2)
    quantity_done = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    notes = models.TextField(blank=True)
    photographs = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "service_progress"
        ordering = ["-reported_at"]


class ServiceCertification(models.Model):
    """The gate that releases billing.

    Separating "someone reported 60%" from "someone certified 60% as billable"
    is the whole point. Running-bill certification is the norm for
    subcontractors, not an edge case.
    """

    service_order = models.ForeignKey(
        ServiceOrder, on_delete=models.CASCADE, related_name="certifications"
    )
    running_bill_number = models.PositiveIntegerField()
    certified_quantity = models.DecimalField(max_digits=18, decimal_places=4)
    certified_value = models.DecimalField(max_digits=18, decimal_places=2)
    is_final = models.BooleanField(default=False)
    certified_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="certifications"
    )
    certified_at = models.DateTimeField()
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "service_certification"
        ordering = ["service_order", "running_bill_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_order", "running_bill_number"], name="uniq_running_bill"
            )
        ]

    def __str__(self) -> str:
        return f"{self.service_order.number} RA-{self.running_bill_number}"


class VendorBill(models.Model):
    """Raised on certification for a service line, or on a verified receipt for
    a supply line. There is no goods receipt behind a service bill — there is
    nothing physical to receive."""

    DRAFT = "draft"
    POSTED = "posted"
    PAID = "paid"
    HELD = "held"
    STATUS_CHOICES = [
        (DRAFT, "Draft"), (POSTED, "Posted"), (PAID, "Paid"), (HELD, "Held")
    ]

    vendor = models.ForeignKey(
        "procurement.Vendor", on_delete=models.PROTECT, related_name="bills"
    )
    project = models.ForeignKey(
        "core.Project", on_delete=models.PROTECT, related_name="vendor_bills"
    )
    lot = models.ForeignKey(
        "sales.Lot", on_delete=models.PROTECT, null=True, blank=True, related_name="vendor_bills"
    )
    number = models.CharField(max_length=64, unique=True)
    source_type = models.CharField(max_length=64)
    source_id = models.BigIntegerField()
    bill_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=DRAFT)

    class Meta:
        db_table = "vendor_bill"
        ordering = ["-bill_date"]
