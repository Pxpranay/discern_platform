"""The three append-only ledgers.

Design principle 1 (process design §2): cost, committed quantity and stock on
hand are *derived* by summing entries. No process ever increments a stored
total, so nothing can silently drift.

Append-only is enforced by a database trigger rather than by revoking UPDATE
and DELETE grants. Grants are bypassed by superusers and by any migration run
as the table owner; a ``BEFORE UPDATE OR DELETE`` trigger is not. ``TRUNCATE``
still works, which is what Django's test teardown uses, so this does not make
the tables untestable.
"""

from decimal import Decimal

from django.db import models


class AppendOnlyQuerySet(models.QuerySet):
    """Refuses the bulk paths that bypass ``Model.save``/``delete``."""

    def update(self, **kwargs):
        raise NotImplementedError(
            f"{self.model.__name__} is an append-only ledger. "
            f"Post a compensating entry instead of updating."
        )

    def delete(self):
        raise NotImplementedError(
            f"{self.model.__name__} is an append-only ledger. "
            f"Post a reversing entry instead of deleting."
        )


class AppendOnlyModel(models.Model):
    """Base for ledger tables. Rows may be inserted, never changed."""

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise NotImplementedError(
                f"{self.__class__.__name__} is append-only; "
                f"entry {self.pk} cannot be modified."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise NotImplementedError(
            f"{self.__class__.__name__} is append-only; "
            f"entry {self.pk} cannot be deleted."
        )


class CommitmentEntry(AppendOnlyModel):
    """One signed authorization against a BOQ line (process design §3.6).

    Positive delta consumes headroom, negative releases it. Because releasing
    is consumption with the opposite sign, returns, cancellations and
    amendments net out arithmetically — which is the defect this fixes in the
    earlier design, where a returned line's headroom was lost permanently.
    """

    boq_line = models.ForeignKey(
        "core.BoqLine", on_delete=models.PROTECT, related_name="commitments"
    )
    project = models.ForeignKey(
        "core.Project", on_delete=models.PROTECT, related_name="commitments"
    )
    document_type = models.CharField(max_length=64)
    document_id = models.BigIntegerField()
    qty_delta = models.DecimalField(max_digits=18, decimal_places=4)
    reason = models.TextField()
    actor = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "commitment_entry"
        indexes = [
            models.Index(fields=["boq_line"]),
            models.Index(fields=["document_type", "document_id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(qty_delta=Decimal("0")),
                name="commitment_delta_nonzero",
            )
        ]

    def __str__(self) -> str:
        return f"{self.document_type}:{self.document_id} {self.qty_delta:+}"


class CostEntry(AppendOnlyModel):
    """One cost or revenue movement against a project and lot.

    Every cost-bearing event in the process posts here, which is what makes
    profitability a GROUP BY rather than a reporting pipeline — and what
    guarantees no category of real spend can fail to reach the figure
    (process design §3.7).
    """

    MATERIAL = "MATERIAL"
    FABRICATION = "FABRICATION"
    SUBCONTRACT = "SUBCONTRACT"
    SITE_EXPENSE = "SITE_EXPENSE"
    STOCK_IN = "STOCK_IN"
    STOCK_OUT = "STOCK_OUT"
    REVENUE = "REVENUE"
    CATEGORY_CHOICES = [
        (MATERIAL, "Material"),
        (FABRICATION, "Fabrication"),
        (SUBCONTRACT, "Subcontract"),
        (SITE_EXPENSE, "Site expense"),
        (STOCK_IN, "Stock transferred in"),
        (STOCK_OUT, "Stock transferred out"),
        (REVENUE, "Revenue"),
    ]

    project = models.ForeignKey(
        "core.Project", on_delete=models.PROTECT, related_name="cost_entries"
    )
    # Nullable only until Phase 1 introduces Lot; it becomes required then.
    lot_id = models.BigIntegerField(null=True, blank=True)
    boq_line = models.ForeignKey(
        "core.BoqLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cost_entries",
    )
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    source_type = models.CharField(max_length=64)
    source_id = models.BigIntegerField()

    #: When it economically occurred.
    effective_date = models.DateField()
    #: When we recorded it. Separate from ``effective_date`` on purpose:
    #: conflating them is how a closed period silently changes.
    posted_at = models.DateTimeField(auto_now_add=True)

    reverses = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals"
    )
    actor = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "cost_entry"
        indexes = [
            models.Index(fields=["project", "category", "effective_date"]),
            models.Index(fields=["lot_id"]),
            models.Index(fields=["source_type", "source_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.category} {self.amount} on project {self.project_id}"


class StockMove(AppendOnlyModel):
    """One movement of stock. On-hand is the sum of moves (process design §3.8).

    A null ``from_location`` is a receipt from outside; a null ``to_location``
    is an issue or return to outside.
    """

    item = models.ForeignKey("core.Item", on_delete=models.PROTECT, related_name="moves")
    from_location = models.ForeignKey(
        "core.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="moves_out",
    )
    to_location = models.ForeignKey(
        "core.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="moves_in",
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    unit_value = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    source_type = models.CharField(max_length=64)
    source_id = models.BigIntegerField()
    effective_at = models.DateTimeField()
    actor = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "stock_move"
        indexes = [
            models.Index(fields=["item", "to_location"]),
            models.Index(fields=["item", "from_location"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=Decimal("0")),
                name="stock_move_qty_positive",
            ),
            # A move from nowhere to nowhere is not a move.
            models.CheckConstraint(
                condition=~(
                    models.Q(from_location__isnull=True)
                    & models.Q(to_location__isnull=True)
                ),
                name="stock_move_has_endpoint",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} of item {self.item_id}"
