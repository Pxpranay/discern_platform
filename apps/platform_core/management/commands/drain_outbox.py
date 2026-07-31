"""Drain the transactional outbox by hand.

The Celery worker normally does this on a schedule, but Redis is the only
reason this project needs a broker at all. Anyone running without Docker —
and so without Redis — can drain from the command line instead:

    python manage.py drain_outbox

Same code path as the worker, so nothing behaves differently; the events
simply go out when you ask rather than on a timer.
"""

from django.core.management.base import BaseCommand

from apps.platform_core.services import events


class Command(BaseCommand):
    help = "Publish pending outbox events (what the Celery worker does on a schedule)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100,
                            help="Maximum events to publish in this pass (default 100).")

    def handle(self, *args, **options):
        result = events.drain(limit=options["limit"])
        for key, value in sorted(result.items()):
            self.stdout.write(f"  {key:<14} {value}")
        self.stdout.write(self.style.SUCCESS("Outbox drained."))
