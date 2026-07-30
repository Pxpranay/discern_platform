"""Bring an empty database up to a usable, logged-in-able state.

Written to be safe to run on every container start:

* it waits for Postgres rather than assuming it is already accepting
  connections, because ``depends_on`` only waits for the healthcheck the
  first time and a restarted database races the web process;
* ``migrate``, ``seed_roles`` and ``seed_login`` are all idempotent;
* ``demo`` is **not** — it writes a full worked project — so it runs only
  when there is no project yet. That makes the first start a demo and every
  later start leave your data alone.

    python manage.py bootstrap
"""

import time

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import OperationalError, connection


class Command(BaseCommand):
    help = "Migrate, seed roles and login, and load the demo project if the database is empty."

    def add_arguments(self, parser):
        parser.add_argument(
            "--wait", type=int, default=60,
            help="Seconds to wait for the database before giving up (default 60).",
        )

    def handle(self, *args, **options):
        self._wait_for_database(options["wait"])

        call_command("migrate", interactive=False, verbosity=1)
        call_command("seed_roles")
        call_command("seed_login")

        from apps.core.models import Project

        if Project.objects.exists():
            self.stdout.write(self.style.WARNING(
                "Projects already exist — skipping the demo walkthrough so your data survives."
            ))
        else:
            call_command("demo")

        self.stdout.write(self.style.SUCCESS(
            "\nReady.  http://localhost:8000/   login  demo / discern2026"
        ))

    def _wait_for_database(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while True:
            try:
                connection.ensure_connection()
                return
            except OperationalError:
                if time.monotonic() >= deadline:
                    raise
                self.stdout.write("waiting for the database…")
                connection.close()
                time.sleep(2)
