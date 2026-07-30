"""View-level access control.

Hiding a nav item is presentation, not security. Every gated view checks the
capability itself, so typing the URL is refused the same way.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.shortcuts import render


def requires(capability: str):
    """Refuse the view unless the user holds ``capability``.

    Administrators hold everything by definition (``AppUser.has_capability``).
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not request.user.has_capability(capability):
                return render(
                    request,
                    "ui/denied.html",
                    {"capability": capability, "nav": None},
                    status=403,
                )
            return view(request, *args, **kwargs)

        wrapper.required_capability = capability
        return wrapper

    return decorator


def require_action(request, capability: str) -> None:
    """Guard a single operation inside a view the user can otherwise see."""
    from apps.platform_core.exceptions import DomainError

    if not request.user.has_capability(capability):
        from apps.accounts.capabilities import label_for

        raise DomainError(
            f"You do not have permission to {label_for(capability).lower()}."
        )
