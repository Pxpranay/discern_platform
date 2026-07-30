"""Minimal domain entities the Phase 0 ledgers need to reference.

These are deliberately thin. Phase 1 extends ``Project`` with its order, client,
budget and schedule; Phase 2 extends ``BoqLine`` with its revision, section, lot
and route (build plan §3, §4). What is here is only what the commitment, cost
and stock ledgers must point at in order to be real and testable now.

Keeping them in a separate app means the later phases add fields to these
models rather than rehoming every foreign key that already points at them.
"""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class ItemCategory(models.Model):
    """Item grouping, and the home of the BOQ ceiling's wastage tolerance.

    Decision #1 in the decisions register: tolerance is configured per item
    category and defaults to zero. A global percentage would be wrong in both
    directions — too loose on a pump, too tight on cement.
    """

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    wastage_tolerance_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Percentage above the BOQ quantity the ceiling permits. Default 0.",
    )

    class Meta:
        db_table = "item_category"
        verbose_name_plural = "item categories"

    def __str__(self) -> str:
        return self.name


class Item(models.Model):
    GOODS = "goods"
    SERVICE = "service"
    TYPE_CHOICES = [(GOODS, "Goods"), (SERVICE, "Service")]

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=256)
    category = models.ForeignKey(
        ItemCategory, on_delete=models.PROTECT, related_name="items", null=True, blank=True
    )
    item_type = models.CharField(max_length=16, choices=TYPE_CHOICES, default=GOODS)
    uom = models.CharField(max_length=32, default="nos")

    class Meta:
        db_table = "item"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Project(models.Model):
    """Phase 0 stub. Phase 1 adds order, client, budget, schedule, PM."""

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=256)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "project"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Location(models.Model):
    """Stock location. Project sites are locations with ``project`` set,
    which is what scopes stock to a project (process design §5.4)."""

    SITE = "site"
    YARD = "yard"
    WORKS = "works"
    VENDOR = "vendor"
    TRANSIT = "transit"
    KIND_CHOICES = [
        (SITE, "Project site"),
        (YARD, "Central yard"),
        (WORKS, "Works / fabrication"),
        (VENDOR, "Vendor premises (job work)"),
        (TRANSIT, "In transit"),
    ]

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=256)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=SITE)
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, null=True, blank=True, related_name="locations"
    )
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="children"
    )

    class Meta:
        db_table = "location"

    def __str__(self) -> str:
        return self.name


class BoqLine(models.Model):
    """Phase 0 stub of the BOQ line the quantity ceiling is enforced against.

    Phase 2 adds the revision, section, lot and route. The ceiling only needs
    the project, the item and the authorized quantity, so the commitment ledger
    and its property tests can be built and proven now — which is the point of
    building the platform foundations first (build plan §2).
    """

    SUPPLY = "SUPPLY"
    FABRICATE = "FABRICATE"
    SERVICE = "SERVICE"
    ROUTE_CHOICES = [(SUPPLY, "Supply"), (FABRICATE, "Fabricate"), (SERVICE, "Service")]

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="boq_lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="boq_lines")
    description = models.TextField(blank=True)
    quantity = models.DecimalField(
        max_digits=18, decimal_places=4, validators=[MinValueValidator(Decimal("0"))]
    )
    uom = models.CharField(max_length=32, default="nos")
    route = models.CharField(max_length=16, choices=ROUTE_CHOICES, default=SUPPLY)

    class Meta:
        db_table = "boq_line"

    def __str__(self) -> str:
        return f"BOQ line {self.pk}: {self.quantity} {self.uom} of {self.item_id}"
