"""Site expenses.

Process design §4.11. A site carries running costs outside the BOQ entirely
that still consume margin. They post to the **same cost ledger** as material
and subcontract, which is the structural fix for the gap the earlier design
documented — an entire category of real spend that could not reach the
profitability figure at all.
"""

from django.db import models


class ExpenseCategory(models.TextChoices):
    ROOM_RENT = "room_rent", "Room rent"
    WATER = "water", "Water"
    CONVEYANCE = "conveyance", "Site conveyance"
    FOODING = "fooding", "Site fooding"
    MISCELLANEOUS = "miscellaneous", "Site miscellaneous"


class SiteExpense(models.Model):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (SUBMITTED, "Submitted"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
    ]

    project = models.ForeignKey(
        "core.Project", on_delete=models.PROTECT, related_name="site_expenses"
    )
    lot = models.ForeignKey(
        "sales.Lot", on_delete=models.PROTECT, null=True, blank=True, related_name="site_expenses"
    )
    category = models.CharField(max_length=24, choices=ExpenseCategory.choices)
    description = models.CharField(max_length=256, blank=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    expense_date = models.DateField()

    incurred_by = models.CharField(max_length=128, blank=True)
    submitted_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="site_expenses"
    )
    approved_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=SUBMITTED)
    receipt_reference = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "site_expense"
        ordering = ["-expense_date"]

    def __str__(self) -> str:
        return f"{self.get_category_display()} {self.amount} on {self.expense_date}"
