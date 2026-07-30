"""Leads, opportunities and the pipeline.

Construction work needs a site visit and a rough-order technical estimate
before it is a firm opportunity, so those are explicit pipeline stages rather
than something tracked in someone's notes (process design §4.1).
"""

from decimal import Decimal

from django.db import models


class Lead(models.Model):
    ENQUIRY = "enquiry"
    REFERRAL = "referral"
    TENDER = "tender"
    SOURCE_CHOICES = [
        (ENQUIRY, "Site enquiry"),
        (REFERRAL, "Referral"),
        (TENDER, "Tender notice"),
    ]

    NEW = "new"
    ASSIGNED = "assigned"
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    STAGE_CHOICES = [
        (NEW, "New"),
        (ASSIGNED, "Assigned"),
        (QUALIFIED, "Qualified"),
        (REJECTED, "Rejected"),
    ]

    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=ENQUIRY)
    client_name = models.CharField(max_length=256)
    contact_name = models.CharField(max_length=128, blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    contact_email = models.EmailField(blank=True)
    site_location = models.CharField(max_length=256, blank=True)
    work_type = models.CharField(max_length=64, blank=True)
    territory = models.CharField(max_length=64, blank=True)
    estimated_value = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    assigned_to = models.ForeignKey(
        "accounts.AppUser",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="leads",
    )
    stage = models.CharField(max_length=16, choices=STAGE_CHOICES, default=NEW)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "lead"

    def __str__(self) -> str:
        return f"{self.client_name} ({self.work_type or 'unspecified'})"


class LeadAssignmentRule(models.Model):
    """Auto-assignment by work type and/or territory.

    First matching rule by ``sequence`` wins. A rule with both fields blank is
    the catch-all, which is how a lead never ends up unassigned and unnoticed.
    """

    work_type = models.CharField(max_length=64, blank=True)
    territory = models.CharField(max_length=64, blank=True)
    assign_to = models.ForeignKey(
        "accounts.AppUser", on_delete=models.CASCADE, related_name="assignment_rules"
    )
    sequence = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "lead_assignment_rule"
        ordering = ["sequence"]

    def matches(self, lead: Lead) -> bool:
        if self.work_type and self.work_type != lead.work_type:
            return False
        if self.territory and self.territory != lead.territory:
            return False
        return True


class Opportunity(models.Model):
    NEW = "new"
    SITE_VISIT = "site_visit"
    ESTIMATING = "estimating"
    QUOTED = "quoted"
    WON = "won"
    LOST = "lost"
    STAGE_CHOICES = [
        (NEW, "New"),
        (SITE_VISIT, "Site visit"),
        (ESTIMATING, "Estimating"),
        (QUOTED, "Quoted"),
        (WON, "Won"),
        (LOST, "Lost"),
    ]

    lead = models.ForeignKey(
        Lead, on_delete=models.PROTECT, null=True, blank=True, related_name="opportunities"
    )
    client = models.ForeignKey(
        "sales.Client", on_delete=models.PROTECT, related_name="opportunities"
    )
    name = models.CharField(max_length=256)
    owner = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="opportunities"
    )
    stage = models.CharField(max_length=16, choices=STAGE_CHOICES, default=NEW)
    rough_order_value = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0")
    )
    expected_close = models.DateField(null=True, blank=True)
    won_at = models.DateTimeField(null=True, blank=True)
    lost_at = models.DateTimeField(null=True, blank=True)
    lost_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "opportunity"
        verbose_name_plural = "opportunities"

    def __str__(self) -> str:
        return self.name

    @property
    def is_open(self) -> bool:
        return self.stage not in (self.WON, self.LOST)


class SiteVisit(models.Model):
    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.CASCADE, related_name="site_visits"
    )
    visited_at = models.DateTimeField()
    visited_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="site_visits"
    )
    findings = models.TextField(blank=True)
    rough_estimate = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )

    class Meta:
        db_table = "site_visit"


class Activity(models.Model):
    CALL = "call"
    MEETING = "meeting"
    EMAIL = "email"
    FOLLOW_UP = "follow_up"
    TYPE_CHOICES = [
        (CALL, "Call"),
        (MEETING, "Meeting"),
        (EMAIL, "Email"),
        (FOLLOW_UP, "Follow up"),
    ]

    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.CASCADE, related_name="activities"
    )
    activity_type = models.CharField(max_length=16, choices=TYPE_CHOICES, default=FOLLOW_UP)
    due_date = models.DateField()
    assigned_to = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="activities"
    )
    notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "activity"
        ordering = ["due_date"]

    @property
    def is_open(self) -> bool:
        return self.completed_at is None
