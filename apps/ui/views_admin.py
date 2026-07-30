"""Administration — users, roles and project assignments.

Everything here is gated behind ``admin:manage``. Changes are audited, because
who could do what, and from when, is exactly the kind of question that gets
asked long after the change.
"""

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.capabilities import ALL_CODES, grouped, label_for, unknown
from apps.accounts.models import AppUser, ProjectAssignment, Role, UserRole
from apps.core.models import Project
from apps.platform_core.models import AdminOverride, AuditEntry

from .access import requires


def _audit(request, entity, action, before=None, after=None, reason=""):
    AuditEntry.objects.create(
        actor=request.user,
        entity_type=entity.__class__.__name__,
        entity_id=entity.pk,
        action=action,
        before=before,
        after=after,
        reason=reason,
    )


@requires("admin:manage")
def admin_home(request):
    return render(
        request,
        "ui/admin/index.html",
        {
            "users": AppUser.objects.annotate(
                role_count=Count("user_roles", distinct=True),
                project_count=Count("project_assignments", distinct=True),
            ).order_by("username"),
            "roles": Role.objects.annotate(user_count=Count("user_roles")).order_by("code"),
            "overrides": AdminOverride.objects.select_related("actor")[:10],
            "nav": "admin",
        },
    )


# ------------------------------------------------------------------- roles
@requires("admin:manage")
def role_list(request):
    if request.method == "POST":
        code = (request.POST.get("code") or "").strip().lower().replace(" ", "_")
        name = (request.POST.get("name") or "").strip()
        if not code or not name:
            messages.error(request, "A role needs both a code and a name.")
        elif Role.objects.filter(code=code).exists():
            messages.error(request, f"A role with code “{code}” already exists.")
        else:
            role = Role.objects.create(code=code, name=name, capabilities=[])
            _audit(request, role, "role.create", after={"code": code, "name": name})
            messages.success(request, f"Role “{name}” created. Now choose its permissions.")
            return redirect("role_detail", pk=role.pk)
        return redirect("role_list")

    return render(
        request,
        "ui/admin/roles.html",
        {
            "roles": Role.objects.annotate(user_count=Count("user_roles")).order_by("code"),
            "nav": "admin",
        },
    )


@requires("admin:manage")
def role_detail(request, pk):
    role = get_object_or_404(Role, pk=pk)

    if request.method == "POST":
        if request.POST.get("action") == "delete":
            if role.user_roles.exists():
                messages.error(
                    request,
                    f"“{role.name}” is still held by {role.user_roles.count()} user(s). "
                    f"Remove it from them first.",
                )
                return redirect("role_detail", pk=role.pk)
            _audit(request, role, "role.delete", before={"code": role.code})
            name = role.name
            role.delete()
            messages.success(request, f"Role “{name}” deleted.")
            return redirect("role_list")

        selected = [c for c in request.POST.getlist("capabilities") if c in ALL_CODES]
        rejected = unknown(request.POST.getlist("capabilities"))
        before = list(role.capabilities or [])
        role.name = (request.POST.get("name") or role.name).strip()
        role.capabilities = sorted(selected)
        role.save(update_fields=["name", "capabilities"])
        _audit(request, role, "role.permissions", before={"capabilities": before},
               after={"capabilities": role.capabilities})

        if rejected:
            messages.error(request, f"Ignored unknown capabilities: {', '.join(sorted(rejected))}")
        messages.success(request, f"Permissions saved for “{role.name}”.")
        return redirect("role_detail", pk=role.pk)

    held = set(role.capabilities or [])
    groups = []
    for group, caps in grouped().items():
        groups.append(
            {
                "name": group,
                "caps": [{**c, "held": c["code"] in held} for c in caps],
            }
        )

    return render(
        request,
        "ui/admin/role_detail.html",
        {
            "role": role,
            "groups": groups,
            "holders": AppUser.objects.filter(user_roles__role=role).order_by("username"),
            "nav": "admin",
        },
    )


# ------------------------------------------------------------------- users
@requires("admin:manage")
def user_list(request):
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        if not username:
            messages.error(request, "A username is required.")
        elif AppUser.objects.filter(username=username).exists():
            messages.error(request, f"“{username}” already exists.")
        else:
            user = AppUser.objects.create(
                username=username,
                email=(request.POST.get("email") or "").strip(),
                first_name=(request.POST.get("first_name") or "").strip(),
            )
            password = request.POST.get("password") or ""
            if password:
                user.set_password(password)
                user.save(update_fields=["password"])
            _audit(request, user, "user.create", after={"username": username})
            messages.success(request, f"User “{username}” created.")
            return redirect("user_detail", pk=user.pk)
        return redirect("user_list")

    return render(
        request,
        "ui/admin/users.html",
        {
            "users": AppUser.objects.annotate(
                role_count=Count("user_roles", distinct=True),
                project_count=Count("project_assignments", distinct=True),
            ).order_by("username"),
            "nav": "admin",
        },
    )


@requires("admin:manage")
def user_detail(request, pk):
    account = get_object_or_404(AppUser, pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "roles":
            chosen = set(request.POST.getlist("roles"))
            before = sorted(account.user_roles.values_list("role__code", flat=True))
            account.user_roles.exclude(role__pk__in=chosen).delete()
            for role_pk in chosen:
                UserRole.objects.get_or_create(user=account, role_id=role_pk)
            after = sorted(account.user_roles.values_list("role__code", flat=True))
            _audit(request, account, "user.roles", before={"roles": before}, after={"roles": after})
            messages.success(request, "Roles updated.")

        elif action == "projects":
            project_id = request.POST.get("project")
            role_id = request.POST.get("project_role")
            if project_id and role_id:
                ProjectAssignment.objects.get_or_create(
                    user=account, project_id=project_id, role_id=role_id
                )
                _audit(request, account, "user.project_assigned", after={"project": project_id})
                messages.success(request, "Project assigned.")
            else:
                messages.error(request, "Pick both a project and a role.")

        elif action == "unassign":
            ProjectAssignment.objects.filter(
                pk=request.POST.get("assignment"), user=account
            ).delete()
            messages.info(request, "Project assignment removed.")

        elif action == "password":
            password = request.POST.get("password") or ""
            if len(password) < 8:
                messages.error(request, "Use at least 8 characters.")
            else:
                account.set_password(password)
                account.save(update_fields=["password"])
                _audit(request, account, "user.password_reset")
                messages.success(request, "Password reset.")

        elif action == "toggle_active":
            if account.pk == request.user.pk:
                messages.error(request, "You cannot deactivate your own account.")
            else:
                account.is_active = not account.is_active
                account.save(update_fields=["is_active"])
                _audit(request, account, "user.active", after={"is_active": account.is_active})
                messages.success(
                    request, f"“{account.username}” {'activated' if account.is_active else 'deactivated'}."
                )

        elif action == "toggle_admin":
            if account.pk == request.user.pk and account.is_administrator:
                messages.error(
                    request,
                    "You cannot remove your own Administrator rights — you would lose "
                    "access to this screen.",
                )
            else:
                account.is_administrator = not account.is_administrator
                account.save(update_fields=["is_administrator"])
                _audit(request, account, "user.administrator",
                       after={"is_administrator": account.is_administrator})
                messages.success(request, "Administrator rights updated.")

        return redirect("user_detail", pk=account.pk)

    effective = sorted(account.capabilities())
    return render(
        request,
        "ui/admin/user_detail.html",
        {
            "account": account,
            "roles": Role.objects.order_by("code"),
            "held_roles": set(account.user_roles.values_list("role_id", flat=True)),
            "assignments": account.project_assignments.select_related("project", "role"),
            "projects": Project.objects.order_by("code")[:200],
            "effective": [{"code": c, "label": label_for(c)} for c in effective],
            "is_self": account.pk == request.user.pk,
            "nav": "admin",
        },
    )
