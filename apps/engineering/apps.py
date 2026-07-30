from django.apps import AppConfig


class EngineeringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.engineering"
    label = "engineering"

    def ready(self):
        from . import services  # noqa: F401
