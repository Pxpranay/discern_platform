"""Small helpers for building test fixtures. Deliberately not a factory
library — Phase 0 has few enough entities that plain functions are clearer."""

import itertools
from decimal import Decimal

from apps.accounts.models import AppUser, Role
from apps.core.models import BoqLine, Item, ItemCategory, Location, Project

_counter = itertools.count(1)


def make_user(username=None, is_administrator=False, **kwargs) -> AppUser:
    n = next(_counter)
    return AppUser.objects.create(
        username=username or f"user{n}",
        email=f"user{n}@discern.test",
        is_administrator=is_administrator,
        **kwargs,
    )


def make_role(code=None, capabilities=None) -> Role:
    n = next(_counter)
    return Role.objects.create(
        code=code or f"role{n}",
        name=(code or f"role{n}").replace("_", " ").title(),
        capabilities=capabilities or [],
    )


def make_project(code=None) -> Project:
    n = next(_counter)
    return Project.objects.create(code=code or f"proj{n}", name=f"Project {n}")


def make_item(category=None, code=None) -> Item:
    n = next(_counter)
    return Item.objects.create(
        code=code or f"item{n}", name=f"Item {n}", category=category, uom="nos"
    )


def make_location(project=None, kind=Location.SITE) -> Location:
    n = next(_counter)
    return Location.objects.create(
        code=f"loc{n}", name=f"Location {n}", kind=kind, project=project
    )


def make_boq_line(quantity=Decimal("100"), project=None, category=None, **kwargs) -> BoqLine:
    project = project or make_project()
    item = make_item(category=category)
    return BoqLine.objects.create(
        project=project, item=item, quantity=Decimal(quantity), uom="nos", **kwargs
    )
