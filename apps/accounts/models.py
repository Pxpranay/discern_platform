"""Identity, roles and project scoping.

Access is role-based and scoped per project (process design §5.5). A role
grants capabilities on document types; a project assignment determines which
projects those capabilities reach. Roles compose — one person may hold
Purchase Manager and cover Construction duties on a smaller project.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models


class AppUser(AbstractUser):
    phone = models.CharField(max_length=32, blank=True)

    #: Administrators bypass project scoping and may override record locks
    #: (process design §5.2). Deliberately distinct from ``is_superuser`` so
    #: the business role is not entangled with Django's own admin plumbing.
    is_administrator = models.BooleanField(default=False)

    class Meta:
        db_table = "app_user"

    def __str__(self) -> str:
        return self.get_full_name() or self.username

    def capabilities(self) -> set[str]:
        """Union of capabilities across every role this user holds."""
        caps: set[str] = set()
        for user_role in self.user_roles.select_related("role"):
            caps.update(user_role.role.capabilities or [])
        return caps

    def has_capability(self, capability: str) -> bool:
        if self.is_administrator:
            return True
        return capability in self.capabilities()

    def assigned_project_ids(self) -> list[int]:
        return list(
            self.project_assignments.values_list("project_id", flat=True).distinct()
        )


class Role(models.Model):
    """A named role with a set of capability strings.

    Capabilities are ``"<document_type>:<verb>"`` strings, e.g.
    ``"purchase_order:approve"``. Kept as data rather than code so the role
    catalogue in process design §8 is configurable without a deploy.
    """

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    capabilities = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "role"
        ordering = ["code"]

    def __str__(self) -> str:
        return self.name


class UserRole(models.Model):
    user = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="user_roles")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_role"
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="uniq_user_role")
        ]


class ProjectAssignment(models.Model):
    """Scopes a user's capabilities to specific projects."""

    user = models.ForeignKey(
        AppUser, on_delete=models.CASCADE, related_name="project_assignments"
    )
    project = models.ForeignKey(
        "core.Project", on_delete=models.CASCADE, related_name="assignments"
    )
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="+")
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "project_assignment"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "project", "role"], name="uniq_project_assignment"
            )
        ]
