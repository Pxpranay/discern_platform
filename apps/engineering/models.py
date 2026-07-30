"""BOQ revisions and discipline-owned sections.

Process design §3.3–§3.4. One BOQ per project, revised as a whole. A revision
is an immutable snapshot of every line, Goods and Service together, with an
author, a release date and a computed diff against the previously released
revision.

Sections are owned by discipline rather than being separate documents: the
Design Manager owns Goods, the Construction Manager owns Service, and each
signs off their own. A section with no lines is marked *not applicable* and
does not hold up the release — which is what prevents the deadlock the earlier
two-document design had on any materials-only or labour-only project.
"""

from django.db import models

from apps.platform_core.models import Approvable


class Discipline(models.TextChoices):
    GOODS = "goods", "Goods"
    SERVICE = "service", "Service"


class BoqRevision(Approvable):
    DRAFT = "draft"
    SECTIONS_SIGNED = "sections_signed"
    RELEASED = "released"
    SENT_BACK = "sent_back"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (SECTIONS_SIGNED, "Sections signed off"),
        (RELEASED, "Released"),
        (SENT_BACK, "Sent back"),
    ]

    project = models.ForeignKey(
        "core.Project", on_delete=models.PROTECT, related_name="boq_revisions"
    )
    revision_number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT)

    prepared_at = models.DateTimeField(auto_now_add=True)
    released_by = models.ForeignKey(
        "accounts.AppUser",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="released_boq_revisions",
    )
    released_at = models.DateTimeField(null=True, blank=True)
    sent_back_reason = models.TextField(blank=True)

    #: Set when this revision supersedes another. The diff is computed against
    #: the last *released* revision, not merely the previous number.
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="superseded_by"
    )

    audit_fields = ("revision_number", "status")

    class Meta:
        db_table = "boq_revision"
        ordering = ["project", "-revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "revision_number"], name="uniq_project_revision"
            )
        ]

    def __str__(self) -> str:
        return f"{self.project.code} BOQ Rev {self.revision_number}"

    @property
    def is_released(self) -> bool:
        return self.status == self.RELEASED

    @property
    def approval_value(self):
        """BOQs here carry no rates, so there is no value to threshold on.

        Discern's own BOQ documents list SL NO, DESCRIPTION, UNIT and QTY and
        nothing else. Any value-based approval rule on a BOQ revision would
        therefore never fire. See the decisions register.
        """
        return None


class BoqSection(models.Model):
    revision = models.ForeignKey(
        BoqRevision, on_delete=models.CASCADE, related_name="sections"
    )
    discipline = models.CharField(max_length=16, choices=Discipline.choices)
    owner = models.ForeignKey(
        "accounts.AppUser",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="boq_sections",
    )

    signed_off_by = models.ForeignKey(
        "accounts.AppUser",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="signed_boq_sections",
    )
    signed_off_at = models.DateTimeField(null=True, blank=True)

    #: A project with no service scope, or no goods scope, marks the empty
    #: section not applicable. Without this the release waits forever on a
    #: signature nobody can meaningfully give.
    is_not_applicable = models.BooleanField(default=False)

    class Meta:
        db_table = "boq_section"
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "discipline"], name="uniq_revision_discipline"
            )
        ]

    def __str__(self) -> str:
        return f"{self.revision} / {self.get_discipline_display()}"

    @property
    def is_complete(self) -> bool:
        """Signed off, or explicitly not applicable."""
        return self.is_not_applicable or self.signed_off_at is not None


class ReconciliationOutcome(models.Model):
    """What the reconciliation engine decided for one line of a released revision.

    Persisted rather than computed on demand, because it is the instruction
    Procurement acts on, and because "what did we decide, and when" must stay
    answerable after the fact.
    """

    #: The five outcomes of process design §4.5, plus "unchanged".
    #:
    #: Removal is deliberately *not* a sixth kind. A line cut to zero is a
    #: decrease whose routing depends on the same question as any other
    #: decrease — has anything been committed or received against it. Making it
    #: a competing kind duplicated that decision in two places and left the
    #: classifier saying which of the two mattered.
    NEW = "new"
    INCREASED = "increased"
    DECREASED_UNCOMMITTED = "decreased_uncommitted"
    DECREASED_ORDERED = "decreased_ordered"
    DECREASED_RECEIVED = "decreased_received"
    UNCHANGED = "unchanged"
    KIND_CHOICES = [
        (NEW, "New line"),
        (INCREASED, "Quantity increased"),
        (DECREASED_UNCOMMITTED, "Decreased, nothing committed"),
        (DECREASED_ORDERED, "Decreased, already ordered"),
        (DECREASED_RECEIVED, "Decreased, already received"),
        (UNCHANGED, "Unchanged"),
    ]

    REQUEST_DELTA = "request_delta"
    REDUCE_DRAFT = "reduce_draft"
    AMEND_ORDER = "amend_order"
    RETURN_QUEUE = "return_queue"
    NONE = "none"
    ACTION_CHOICES = [
        (REQUEST_DELTA, "Raise procurement request for the delta"),
        (REDUCE_DRAFT, "Reduce or remove the draft request line"),
        (AMEND_ORDER, "Amend or cancel the outstanding order quantity"),
        (RETURN_QUEUE, "Route to the return / redeployment queue"),
        (NONE, "No action"),
    ]

    revision = models.ForeignKey(
        BoqRevision, on_delete=models.CASCADE, related_name="reconciliation_outcomes"
    )
    boq_line = models.ForeignKey(
        "core.BoqLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reconciliation_outcomes",
    )
    previous_line = models.ForeignKey(
        "core.BoqLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    description = models.TextField()
    uom = models.CharField(max_length=32, blank=True)

    previous_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    new_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    delta = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    committed_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    received_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    #: How much must be returned or redeployed, when the cut goes below what
    #: has already physically arrived.
    excess_received = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    #: How much of an outstanding order should be amended down or cancelled.
    order_reduction = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)

    #: The line was cut to zero — dropped from the project, not merely reduced.
    is_removal = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reconciliation_outcome"
        ordering = ["revision", "id"]

    def __str__(self) -> str:
        return f"{self.kind} → {self.action}: {self.description[:40]}"
