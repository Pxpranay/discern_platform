"""Project-scoped querysets.

Scoping is enforced by a base manager rather than by remembering to filter in
each view (architecture §7). A view that forgets to scope is a data leak; a
manager that scopes by default is not something you can forget.
"""

from django.db import models


class ProjectScopedQuerySet(models.QuerySet):
    """Queryset for any model reachable from a single project.

    Models using this must expose a ``project`` FK, or override
    ``project_lookup`` on the manager to name the path to it.
    """

    project_lookup = "project_id"

    def for_user(self, user):
        """Restrict to the projects this user is assigned to.

        Administrators see everything. Everyone else sees only their
        assignments — including users with no assignments at all, who
        correctly see nothing.
        """
        if getattr(user, "is_administrator", False):
            return self
        return self.filter(**{f"{self.project_lookup}__in": user.assigned_project_ids()})


class ProjectScopedManager(models.Manager.from_queryset(ProjectScopedQuerySet)):
    """Default manager for project-scoped models."""
