"""Test settings. Adds the test-support app so platform abstractions such as
``Approvable`` can be exercised against a concrete model without shipping one
in the production schema."""

from .settings import *  # noqa: F401,F403
from .settings import INSTALLED_APPS

INSTALLED_APPS = INSTALLED_APPS + ["apps.testsupport"]

CELERY_TASK_ALWAYS_EAGER = True
