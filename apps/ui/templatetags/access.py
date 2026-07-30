"""Template helper for capability checks, used to hide nav the user cannot open."""

from django import template

register = template.Library()


@register.filter
def can(user, capability: str) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    return user.has_capability(capability)
