"""Create the default role set from the process design's role table."""

from django.core.management.base import BaseCommand

from apps.accounts.capabilities import DEFAULT_ROLES
from apps.accounts.models import Role


class Command(BaseCommand):
    help = "Create or refresh the default roles."

    def handle(self, *args, **options):
        for code, name, caps in DEFAULT_ROLES:
            role, created = Role.objects.get_or_create(
                code=code, defaults={"name": name, "capabilities": caps}
            )
            if not created:
                role.name = name
                role.capabilities = caps
                role.save(update_fields=["name", "capabilities"])
            self.stdout.write(
                f"  {'created' if created else 'updated'}  {code}  ({len(caps)} capabilities)"
            )
        self.stdout.write(self.style.SUCCESS(f"{len(DEFAULT_ROLES)} roles ready."))
