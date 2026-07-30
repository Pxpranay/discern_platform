"""Create a demo login with every capability, for walking through the UI."""

from django.core.management.base import BaseCommand

from apps.accounts.models import AppUser, Role

CAPS = [
    "order:approve_kickoff",
    "project:extend_schedule",
    "boq_revision:release",
]


class Command(BaseCommand):
    help = "Create or reset the demo UI login (demo / discern2026)."

    def handle(self, *args, **options):
        role, _ = Role.objects.get_or_create(
            code="demo_all",
            defaults={"name": "Demo — all capabilities", "capabilities": CAPS},
        )
        role.capabilities = CAPS
        role.save(update_fields=["capabilities"])

        user, created = AppUser.objects.get_or_create(
            username="demo", defaults={"email": "demo@discern.test", "is_administrator": True}
        )
        user.is_administrator = True
        user.is_staff = True
        user.is_superuser = True
        user.set_password("discern2026")
        user.save()
        user.user_roles.get_or_create(role=role)

        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Reset'} login  demo / discern2026"
        ))
