"""Celery tasks. The outbox drain is the only one Phase 0 needs."""

from celery import shared_task

from .services import events


@shared_task(name="platform.drain_outbox")
def drain_outbox(limit: int = 100) -> dict:
    return events.drain(limit=limit)
