"""Transactional outbox.

Design principle 5: hand-offs are events, and events are durable. The event row
is written in the same transaction as the state change that raised it, so a
crash between "order confirmed" and "receipt expected" cannot leave the two
permanently out of step — which is the failure the earlier design's automation
rules had no answer for.
"""

from django.db import models


class OutboxEvent(models.Model):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"
    DEAD = "dead"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (PROCESSED, "Processed"),
        (FAILED, "Failed, will retry"),
        (DEAD, "Dead-lettered"),
    ]

    event_name = models.CharField(max_length=128)
    payload = models.JSONField(default=dict)

    #: Deduplicates delivery. Handlers are called at least once, so a handler
    #: that must not run twice relies on this being set by the emitter.
    idempotency_key = models.CharField(max_length=200, null=True, blank=True, unique=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "outbox_event"
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["event_name"]),
        ]
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.event_name} [{self.status}]"


class Notification(models.Model):
    """In-app notification. The fan-out target for events such as an excess
    stock flag reaching three dashboards at once (process design §4.12)."""

    user = models.ForeignKey(
        "accounts.AppUser", on_delete=models.CASCADE, related_name="notifications"
    )
    event_name = models.CharField(max_length=128, blank=True)
    title = models.CharField(max_length=256)
    body = models.TextField(blank=True)
    entity_type = models.CharField(max_length=128, blank=True)
    entity_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notification"
        ordering = ["-created_at"]
