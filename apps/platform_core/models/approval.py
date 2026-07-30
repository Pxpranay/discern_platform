"""Approval engine and record locking.

Approval is a platform service, not per-screen logic (design principle 4).
One engine enforces the rules, locks the document on approval, and audits every
override — so the platform cannot end up in the state the earlier design named
in its own governance section, where some records locked hard and some merely
hid a button.
"""

from django.db import models
from django.utils import timezone

from ..exceptions import DomainError, MissingReason, RecordLocked


class AuditEntry(models.Model):
    """Append-only record of a mutation to an approval-carrying model."""

    actor = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="+"
    )
    entity_type = models.CharField(max_length=128)
    entity_id = models.BigIntegerField()
    action = models.CharField(max_length=64)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_entry"
        indexes = [models.Index(fields=["entity_type", "entity_id"])]


class AdminOverride(models.Model):
    """An Administrator's edit of a locked record.

    Kept in its own table rather than filtered out of the general audit log,
    because this is the Directors' review queue (process design §6.4) and must
    be trivially listable. Permitted, but never quiet.
    """

    actor = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="overrides"
    )
    entity_type = models.CharField(max_length=128)
    entity_id = models.BigIntegerField()
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    reason = models.TextField()
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "admin_override"
        ordering = ["-at"]

    def __str__(self) -> str:
        return f"override on {self.entity_type} {self.entity_id} by {self.actor_id}"


class Override:
    """Ticket authorizing one write to a locked record.

    Constructed by the caller, validated here, and consumed by
    ``Approvable.save``. Making it an object rather than a boolean flag means
    an override cannot be requested without an actor and a reason.
    """

    def __init__(self, actor, reason: str):
        if not getattr(actor, "is_administrator", False):
            raise DomainError(
                f"{actor} is not an Administrator and cannot override a record lock."
            )
        if not reason or not reason.strip():
            raise MissingReason("An Administrator override requires a reason.")
        self.actor = actor
        self.reason = reason

    def record(self, obj, before: dict, after: dict) -> AdminOverride:
        return AdminOverride.objects.create(
            actor=self.actor,
            entity_type=obj.__class__.__name__,
            entity_id=obj.pk,
            before=before,
            after=after,
            reason=self.reason,
        )


class ApprovalRule(models.Model):
    """Declarative: who must approve what, under which condition.

    ``threshold`` is compared against the document's ``approval_value``.
    A rule with no threshold always applies.
    """

    document_type = models.CharField(max_length=64)
    name = models.CharField(max_length=128)
    threshold = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    required_role = models.ForeignKey(
        "accounts.Role", on_delete=models.PROTECT, related_name="approval_rules"
    )
    sequence = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "approval_rule"
        ordering = ["document_type", "sequence"]

    def applies_to(self, value) -> bool:
        if not self.is_active:
            return False
        if self.threshold is None:
            return True
        return value is not None and value > self.threshold

    def __str__(self) -> str:
        return f"{self.document_type}: {self.name}"


class ApprovalRequest(models.Model):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT_BACK = "sent_back"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
        (SENT_BACK, "Sent back"),
    ]

    document_type = models.CharField(max_length=64)
    document_id = models.BigIntegerField()
    rule = models.ForeignKey(ApprovalRule, on_delete=models.PROTECT, related_name="requests")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    requested_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "approval_request"
        indexes = [models.Index(fields=["document_type", "document_id", "status"])]


class ApprovalAction(models.Model):
    APPROVE = "approve"
    REJECT = "reject"
    SEND_BACK = "send_back"
    ACTION_CHOICES = [
        (APPROVE, "Approve"),
        (REJECT, "Reject"),
        (SEND_BACK, "Send back"),
    ]

    request = models.ForeignKey(
        ApprovalRequest, on_delete=models.CASCADE, related_name="actions"
    )
    actor = models.ForeignKey(
        "accounts.AppUser", on_delete=models.PROTECT, related_name="approval_actions"
    )
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    comments = models.TextField(blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "approval_action"


class ApprovableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        """Refuse bulk updates that would silently bypass the lock.

        ``QuerySet.update`` does not call ``save``, so without this a locked
        record could be edited through the bulk path. The check costs one
        EXISTS query and closes the hole.
        """
        if self.filter(locked_at__isnull=False).exists():
            raise RecordLocked(self.filter(locked_at__isnull=False).first())
        return super().update(**kwargs)


class Approvable(models.Model):
    """Base for any document that carries an approval and locks on it.

    Locking is enforced here, in the domain layer, so it holds for every write
    path — REST, admin, management command or background job. A lock that
    holds in the API and not in the admin screen is not a lock.
    """

    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "accounts.AppUser",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    locked_at = models.DateTimeField(null=True, blank=True)

    objects = ApprovableQuerySet.as_manager()

    class Meta:
        abstract = True

    #: Fields whose values are captured in the override audit trail.
    audit_fields: tuple[str, ...] = ()

    @property
    def is_locked(self) -> bool:
        return self.locked_at is not None

    def snapshot(self) -> dict:
        fields = self.audit_fields or tuple(
            f.name for f in self._meta.fields if not f.is_relation
        )
        return {name: str(getattr(self, name, None)) for name in fields}

    def save(self, *args, override: Override | None = None, **kwargs):
        if self.pk is not None:
            stored = type(self).objects.filter(pk=self.pk).first()
            if stored is not None and stored.locked_at is not None:
                if override is None:
                    raise RecordLocked(self)
                override.record(self, before=stored.snapshot(), after=self.snapshot())
        return super().save(*args, **kwargs)

    def delete(self, *args, override: Override | None = None, **kwargs):
        if self.locked_at is not None and override is None:
            raise RecordLocked(self)
        return super().delete(*args, **kwargs)

    def lock(self, actor) -> None:
        """Mark approved and locked. Called by the approval engine."""
        self.approved_at = timezone.now()
        self.approved_by = actor
        self.locked_at = timezone.now()
        super().save(update_fields=["approved_at", "approved_by", "locked_at"])
