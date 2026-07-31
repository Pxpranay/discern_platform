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

from django.conf import settings
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
        self._create_database_if_missing()
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

    def _create_database_if_missing(self) -> None:
        """`createdb` on Windows means finding psql on a PATH it is usually not
        on. The credentials are already in settings, so do it here instead.

        Connects to the maintenance database `postgres`, which every server has.
        A failure is not fatal — the server may simply not be up yet, and
        `_wait_for_database` gives a better message for that.
        """
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

        db = settings.DATABASES["default"]
        name = db["NAME"]
        try:
            conn = psycopg2.connect(
                dbname="postgres", user=db["USER"], password=db["PASSWORD"],
                host=db["HOST"], port=db["PORT"] or 5432, connect_timeout=5,
            )
        except psycopg2.OperationalError:
            return
        try:
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
                if cur.fetchone():
                    return
                # Identifier, so it cannot be a bound parameter. It comes from
                # our own settings, not from user input.
                cur.execute(f'CREATE DATABASE "{name}"')
                self.stdout.write(self.style.SUCCESS(f"created database {name}"))
        finally:
            conn.close()

    def _wait_for_database(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        announced = False
        while True:
            try:
                connection.ensure_connection()
                return
            except OperationalError as exc:
                if time.monotonic() >= deadline:
                    raise
                if not announced:
                    # Say why on the first failure. Otherwise a wrong password
                    # looks exactly like a database that has not started yet,
                    # and you watch "waiting…" for a minute before finding out.
                    self.stdout.write(self.style.WARNING(
                        f"database not reachable yet: {str(exc).strip().splitlines()[0]}"
                    ))
                    announced = True
                self.stdout.write("waiting for the database…")
                connection.close()
                time.sleep(2)
