from django.apps import AppConfig


class SubcontractsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.subcontracts"
    label = "subcontracts"

    def ready(self):
        from . import services  # noqa: F401
