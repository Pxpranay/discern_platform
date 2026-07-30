"""Master schedule.

Process design §3.11 and §4.3. Named phases with target dates, every date live
and change-logged, and one hard boundary: no phase may be planned beyond the
order's committed delivery date. Raising that ceiling requires a recorded
client agreement.
"""

from django.db import models


class SchedulePhase(models.Model):
    """One named phase of the project's master schedule.

    Procurement is ``kind='procurement'`` with a ``sequence``, so a project
    needing three staged procurement windows — foundation materials first,
    structural steel later, finishing materials last — simply has three rows.
    No special-casing, no configuration.
    """

    SITE_VISIT = "site_visit"
    BOQ_PREP = "boq_prep"
    PROCUREMENT = "procurement"
    CONSTRUCTION = "construction"
    KIND_CHOICES = [
        (SITE_VISIT, "Site visit / requirement assessment"),
        (BOQ_PREP, "Engineering / BOQ preparation"),
        (PROCUREMENT, "Procurement"),
        (CONSTRUCTION, "Construction"),
    ]

    project = models.ForeignKey(
        "core.Project", on_delete=models.CASCADE, related_name="schedule_phases"
    )
    name = models.CharField(max_length=128)
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    sequence = models.PositiveIntegerField(default=1)

    planned_start = models.DateField(null=True, blank=True)
    planned_end = models.DateField()
    actual_start = models.DateField(null=True, blank=True)
    actual_end = models.DateField(null=True, blank=True)
    is_complete = models.BooleanField(default=False)

    class Meta:
        db_table = "schedule_phase"
        ordering = ["project", "sequence"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(planned_start__isnull=True)
                | models.Q(planned_end__gte=models.F("planned_start")),
                name="phase_ends_after_it_starts",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} (by {self.planned_end})"


class PhaseDateChange(models.Model):
    """Change log for a phase's target date.

    Every date on the schedule is a live, editable parameter rather than a
    one-time estimate frozen at kickoff — so the record of what moved, when,
    and who moved it is the thing that makes it accountable.
    """

    phase = models.ForeignKey(
        SchedulePhase, on_delete=models.CASCADE, related_name="date_changes"
    )
    previous_end = models.DateField(null=True, blank=True)
    new_end = models.DateField()
    changed_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "phase_date_change"
        ordering = ["-at"]


class ScheduleExtension(models.Model):
    """A client-agreed extension of the committed delivery date.

    The only thing that raises the schedule ceiling. ``client_agreement_reference``
    is mandatory (decisions register, Tier 2): this is a contractual date, and
    CEO sign-off alone is not evidence the client agreed to move it.
    """

    project = models.ForeignKey(
        "core.Project", on_delete=models.CASCADE, related_name="schedule_extensions"
    )
    previous_committed_date = models.DateField()
    new_committed_date = models.DateField()
    client_agreement_reference = models.TextField()
    authorized_by = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="schedule_extensions"
    )
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "schedule_extension"
        ordering = ["-at"]

    def __str__(self) -> str:
        return f"{self.previous_committed_date} → {self.new_committed_date}"
