"""Concrete models used only to exercise platform abstractions in tests.

Installed by ``config.settings_test`` only, so nothing here reaches a
production schema. The alternative — testing ``Approvable`` against a real
business document — would mean the approval engine could not be proven until
Phase 2 shipped one, which defeats the purpose of building the platform
foundations first.
"""

from decimal import Decimal

from django.db import models

from apps.platform_core.models import Approvable


class DemoDocument(Approvable):
    """A minimal document that carries an approval and locks on it."""

    name = models.CharField(max_length=128)
    value = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))

    audit_fields = ("name", "value")

    class Meta:
        db_table = "demo_document"

    @property
    def approval_value(self):
        return self.value
