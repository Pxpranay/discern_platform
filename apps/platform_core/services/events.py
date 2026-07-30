"""Domain event bus with a transactional outbox.

Emitting writes a row in the caller's transaction; a worker delivers it later.
If the transaction rolls back the event vanishes with it, so a hand-off can
never describe a state change that did not happen — and a crash after commit
cannot lose one that did.
"""

import logging
import traceback
from collections import defaultdict
from typing import Callable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..exceptions import NotInTransaction
from ..models import OutboxEvent

logger = logging.getLogger(__name__)

_HANDLERS: dict[str, list[Callable]] = defaultdict(list)


def handles(event_name: str):
    """Register a handler. Handlers are plain functions taking the payload."""

    def decorator(fn: Callable) -> Callable:
        _HANDLERS[event_name].append(fn)
        return fn

    return decorator


def registered_handlers(event_name: str) -> list[Callable]:
    return list(_HANDLERS.get(event_name, []))


def clear_handlers() -> None:
    """Test helper. Never call from application code."""
    _HANDLERS.clear()


def emit(event_name: str, payload: dict, *, idempotency_key: str | None = None) -> OutboxEvent:
    """Record an event for delivery, inside the caller's transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise NotInTransaction(
            "emit() must run inside transaction.atomic() so the event commits "
            "with the state change that raised it."
        )
    return OutboxEvent.objects.create(
        event_name=event_name, payload=payload, idempotency_key=idempotency_key
    )


def dispatch(event: OutboxEvent) -> bool:
    """Run every handler for one event. Returns True if it is now processed.

    Handlers run in their own transaction. A failure marks the event for retry
    and leaves the others untouched — one bad handler does not stall the queue.
    """
    handlers = registered_handlers(event.event_name)
    try:
        with transaction.atomic():
            for handler in handlers:
                handler(event.payload)
    except Exception:
        event.attempts += 1
        event.last_error = traceback.format_exc()[-4000:]
        max_attempts = getattr(settings, "OUTBOX_MAX_ATTEMPTS", 5)
        event.status = OutboxEvent.DEAD if event.attempts >= max_attempts else OutboxEvent.FAILED
        event.save(update_fields=["attempts", "last_error", "status"])
        logger.exception("outbox handler failed for %s", event.event_name)
        return False

    event.attempts += 1
    event.status = OutboxEvent.PROCESSED
    event.processed_at = timezone.now()
    event.save(update_fields=["attempts", "status", "processed_at"])
    return True


def drain(limit: int = 100) -> dict[str, int]:
    """Deliver pending and retryable events. Returns a small summary."""
    pending = OutboxEvent.objects.filter(
        status__in=[OutboxEvent.PENDING, OutboxEvent.FAILED]
    ).order_by("created_at")[:limit]

    processed = failed = 0
    for event in list(pending):
        if dispatch(event):
            processed += 1
        else:
            failed += 1
    return {"processed": processed, "failed": failed}


def dead_letters():
    """Events that exhausted their retries. Surfaced to an Administrator."""
    return OutboxEvent.objects.filter(status=OutboxEvent.DEAD)


def replay(event: OutboxEvent) -> bool:
    """Return a dead-lettered event to the queue after the cause is fixed."""
    event.status = OutboxEvent.PENDING
    event.attempts = 0
    event.last_error = ""
    event.save(update_fields=["status", "attempts", "last_error"])
    return dispatch(event)
