"""Quotations, orders and lots.

The **Lot** is the pivot of the whole model (process design §3.2). It is the
only thing that carries a price the client agreed to, and every BOQ line,
commitment and cost entry downstream traces to one. Making it a first-class
entity rather than a back-reference on the BOQ line is what turns per-SITC-lot
margin from a reporting project into a ``GROUP BY``.

An order fixes three things the rest of the chain depends on: the agreed price
per lot, the committed delivery date, and the scope each lot represents. Only a
change order moves any of them.
"""

from decimal import Decimal

from django.db import models
from django.db.models import Sum

from apps.platform_core.models import Approvable


class Client(models.Model):
    name = models.CharField(max_length=256)
    gstin = models.CharField(max_length=32, blank=True)
    billing_address = models.TextField(blank=True)
    contact_name = models.CharField(max_length=128, blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    contact_email = models.EmailField(blank=True)
    payment_terms = models.CharField(max_length=128, blank=True)

    class Meta:
        db_table = "client"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class LotKind(models.TextChoices):
    ITEMIZED = "itemized", "Itemized"
    LUMP_SUM_SITC = "lump_sum_sitc", "Lump-sum SITC"


class Quotation(models.Model):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    LOST = "lost"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (SENT, "Sent"),
        (ACCEPTED, "Accepted"),
        (LOST, "Lost"),
    ]

    opportunity = models.ForeignKey(
        "crm.Opportunity",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="quotations",
    )
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="quotations")
    number = models.CharField(max_length=64, unique=True)
    revision = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=DRAFT)
    valid_until = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "quotation"

    def __str__(self) -> str:
        return f"{self.number} rev {self.revision}"

    @property
    def total_value(self) -> Decimal:
        return self.lots.aggregate(t=Sum("price"))["t"] or Decimal("0")


class QuotationLot(models.Model):
    """A priced deliverable on a quotation — itemized, or a single SITC line."""

    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="lots")
    name = models.CharField(max_length=256)
    kind = models.CharField(max_length=20, choices=LotKind.choices, default=LotKind.ITEMIZED)
    price = models.DecimalField(max_digits=18, decimal_places=2)
    scope_description = models.TextField(blank=True)
    sequence = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "quotation_lot"
        ordering = ["sequence"]

    def __str__(self) -> str:
        return self.name


class QuotationLine(models.Model):
    """Only present on an itemized lot. A lump-sum SITC lot has no breakdown —
    that is the whole point of it, and the detail is worked out later in the
    BOQ (process design §4.4)."""

    lot = models.ForeignKey(QuotationLot, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(
        "core.Item", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    description = models.TextField()
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    uom = models.CharField(max_length=32, default="nos")
    rate = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        db_table = "quotation_line"

    @property
    def amount(self) -> Decimal:
        return self.quantity * self.rate


class Order(Approvable):
    """A confirmed client order.

    Carries the **committed delivery date** — the date Discern promised the
    client — which becomes the hard ceiling on the project's master schedule
    (process design §4.3). Nothing but a change order moves it.

    ``Approvable`` gives it the kickoff gate: a confirmed order does not create
    a project until someone marks it approved for kickoff.
    """

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    HELD = "held_pending_review"
    KICKED_OFF = "kicked_off"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (CONFIRMED, "Confirmed"),
        (HELD, "Held pending review"),
        (KICKED_OFF, "Kicked off"),
        (CANCELLED, "Cancelled"),
    ]

    quotation = models.ForeignKey(
        Quotation, on_delete=models.PROTECT, null=True, blank=True, related_name="orders"
    )
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="orders")
    number = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=DRAFT)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    #: The delivery date promised to the client.
    committed_delivery_date = models.DateField(null=True, blank=True)

    kickoff_approved_by = models.ForeignKey(
        "accounts.AppUser",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    kickoff_approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    audit_fields = ("number", "status", "committed_delivery_date")

    class Meta:
        db_table = "sales_order"

    def __str__(self) -> str:
        return self.number

    @property
    def total_value(self) -> Decimal:
        return self.lots.aggregate(t=Sum("price"))["t"] or Decimal("0")

    @property
    def approval_value(self) -> Decimal:
        return self.total_value


class Lot(models.Model):
    """One commercially-priced deliverable on a confirmed order.

    Threads the entire model: BOQ lines, commitments and cost entries all carry
    their lot, which is what makes margin answerable lot by lot rather than only
    as one blended project total.
    """

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="lots")
    name = models.CharField(max_length=256)
    kind = models.CharField(max_length=20, choices=LotKind.choices, default=LotKind.ITEMIZED)

    #: What the client agreed to pay for this lot. Never changed by anything
    #: downstream — BOQ cost flows one way only (process design §3.3).
    price = models.DecimalField(max_digits=18, decimal_places=2)
    scope_description = models.TextField(blank=True)
    sequence = models.PositiveIntegerField(default=1)

    project = models.ForeignKey(
        "core.Project",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lots",
    )

    class Meta:
        db_table = "lot"
        ordering = ["order", "sequence"]

    def __str__(self) -> str:
        return f"{self.order.number} / {self.name}"


class ChangeOrder(Approvable):
    """The only mechanism that alters an order after confirmation.

    Adjusts a lot's price and/or the committed delivery date. Deliberately does
    not touch the BOQ: the BOQ is built from drawings and site conditions, not
    from what the client agreed to pay.
    """

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="change_orders")
    lot = models.ForeignKey(
        Lot, on_delete=models.PROTECT, null=True, blank=True, related_name="change_orders"
    )
    number = models.CharField(max_length=64, unique=True)
    price_delta = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0")
    )
    new_committed_date = models.DateField(null=True, blank=True)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    audit_fields = ("number", "price_delta", "new_committed_date")

    class Meta:
        db_table = "change_order"

    @property
    def approval_value(self) -> Decimal:
        return abs(self.price_delta)


class ClientInvoice(models.Model):
    """Raised against a lot. Posts REVENUE to the cost ledger on issue."""

    DRAFT = "draft"
    ISSUED = "issued"
    PAID = "paid"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (ISSUED, "Issued"),
        (PAID, "Paid"),
        (CANCELLED, "Cancelled"),
    ]

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="invoices")
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="invoices")
    number = models.CharField(max_length=64, unique=True)
    invoice_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=DRAFT)
    issued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "client_invoice"

    def __str__(self) -> str:
        return self.number
