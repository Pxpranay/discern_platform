"""Fabrication — items made to the project's own drawings.

Process design §4.8. A `FABRICATE` BOQ line is built rather than bought: in
Discern's own works, or job-worked out to a vendor and received back. Either
way the finished quantity is capped by the BOQ ceiling before the order exists,
which is why raw materials are deliberately *not* ceiling-checked — they are
components consumed to produce the line, not the line itself.
"""

from decimal import Decimal

from django.db import models

from apps.platform_core.models import Approvable


class BillOfMaterials(models.Model):
    """The recipe for a fabricated item."""

    item = models.ForeignKey("core.Item", on_delete=models.PROTECT, related_name="boms")
    revision = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=256, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "bill_of_materials"
        constraints = [
            models.UniqueConstraint(fields=["item", "revision"], name="uniq_bom_revision")
        ]

    def __str__(self) -> str:
        return f"BOM for {self.item} rev {self.revision}"


class BomComponent(models.Model):
    bom = models.ForeignKey(BillOfMaterials, on_delete=models.CASCADE, related_name="components")
    item = models.ForeignKey("core.Item", on_delete=models.PROTECT, related_name="used_in_boms")
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    uom = models.CharField(max_length=32, default="nos")
    wastage_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))

    class Meta:
        db_table = "bom_component"

    def required_for(self, output_qty: Decimal) -> Decimal:
        base = self.quantity * Decimal(str(output_qty))
        return base + (base * self.wastage_pct / Decimal("100"))


class FabricationMode(models.TextChoices):
    IN_HOUSE = "in_house", "In-house at Discern's works"
    JOB_WORK = "job_work", "Job-worked out to a vendor"


class FabricationOrder(Approvable):
    DRAFT = "draft"
    AWAITING_MATERIAL = "awaiting_material"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (AWAITING_MATERIAL, "Awaiting raw material"),
        (IN_PROGRESS, "In progress"),
        (COMPLETE, "Complete"),
        (CANCELLED, "Cancelled"),
    ]

    project = models.ForeignKey(
        "core.Project", on_delete=models.PROTECT, related_name="fabrication_orders"
    )
    lot = models.ForeignKey(
        "sales.Lot", on_delete=models.PROTECT, null=True, blank=True, related_name="fabrication_orders"
    )
    boq_line = models.ForeignKey(
        "core.BoqLine", on_delete=models.PROTECT, related_name="fabrication_orders"
    )
    item = models.ForeignKey("core.Item", on_delete=models.PROTECT, related_name="+")
    bom = models.ForeignKey(
        BillOfMaterials, on_delete=models.PROTECT, null=True, blank=True, related_name="orders"
    )
    number = models.CharField(max_length=64, unique=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    uom = models.CharField(max_length=32, default="nos")

    mode = models.CharField(
        max_length=16, choices=FabricationMode.choices, default=FabricationMode.IN_HOUSE
    )
    #: Set for job work — components are issued here and the finished item comes back.
    vendor = models.ForeignKey(
        "procurement.Vendor", on_delete=models.PROTECT, null=True, blank=True,
        related_name="job_work_orders",
    )
    job_work_charge = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )

    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=DRAFT)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    audit_fields = ("number", "status", "quantity")

    class Meta:
        db_table = "fabrication_order"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.number

    @property
    def is_job_work(self) -> bool:
        return self.mode == FabricationMode.JOB_WORK


class FabricationStep(models.Model):
    order = models.ForeignKey(FabricationOrder, on_delete=models.CASCADE, related_name="steps")
    sequence = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=128)
    completed_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "fabrication_step"
        ordering = ["sequence"]

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None


class MaterialConsumption(models.Model):
    """Planned against actual.

    Raw materials are exempt from the BOQ ceiling by design, so this is the
    compensating control: a run consuming materially more than its recipe is
    visible rather than absorbed.
    """

    order = models.ForeignKey(
        FabricationOrder, on_delete=models.CASCADE, related_name="consumption"
    )
    item = models.ForeignKey("core.Item", on_delete=models.PROTECT, related_name="+")
    planned_qty = models.DecimalField(max_digits=18, decimal_places=4)
    actual_qty = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal("0"))
    uom = models.CharField(max_length=32, default="nos")

    class Meta:
        db_table = "material_consumption"

    @property
    def variance(self) -> Decimal:
        return self.actual_qty - self.planned_qty

    @property
    def is_over(self) -> bool:
        return self.actual_qty > self.planned_qty
