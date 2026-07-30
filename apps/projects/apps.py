from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.projects"
    label = "projects"

    def ready(self):
        # Importing the service module registers its @events.handles hooks.
        from . import services  # noqa: F401
