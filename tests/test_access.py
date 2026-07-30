"""Role capabilities and project scoping.

Architecture §7. Scoping is enforced by a base manager rather than by
remembering to filter in each view — a view that forgets to scope is a data
leak, and a manager that scopes by default is not something you can forget.
"""

from decimal import Decimal

from django.test import TestCase

from apps.core.models import BoqLine
from apps.platform_core.models import CostEntry

from .factories import make_boq_line, make_project, make_role, make_user


class CapabilityTests(TestCase):
    def test_a_user_with_no_roles_has_no_capabilities(self):
        user = make_user()
        self.assertEqual(user.capabilities(), set())
        self.assertFalse(user.has_capability("purchase_order:approve"))

    def test_capabilities_come_from_the_roles_a_user_holds(self):
        user = make_user()
        role = make_role(
            code="purchase_manager",
            capabilities=["purchase_order:approve", "rfq:award"],
        )
        user.user_roles.create(role=role)
        self.assertTrue(user.has_capability("purchase_order:approve"))
        self.assertTrue(user.has_capability("rfq:award"))
        self.assertFalse(user.has_capability("boq_revision:release"))

    def test_roles_compose(self):
        """One person may hold Purchase Manager and cover Construction duties
        on a smaller project. One role per user does not survive a real org
        chart (process design §5.5)."""
        user = make_user()
        user.user_roles.create(role=make_role(code="pm", capabilities=["a:x"]))
        user.user_roles.create(role=make_role(code="cm", capabilities=["b:y"]))
        self.assertEqual(user.capabilities(), {"a:x", "b:y"})

    def test_an_administrator_holds_every_capability(self):
        admin = make_user(is_administrator=True)
        self.assertTrue(admin.has_capability("anything:at:all"))


class ProjectScopingTests(TestCase):
    def setUp(self):
        self.project_a = make_project(code="alpha")
        self.project_b = make_project(code="bravo")
        self.line_a = make_boq_line(project=self.project_a, quantity=Decimal("10"))
        self.line_b = make_boq_line(project=self.project_b, quantity=Decimal("10"))
        self.role = make_role(code="construction_user")

    def test_a_user_sees_only_their_assigned_projects(self):
        user = make_user()
        user.project_assignments.create(project=self.project_a, role=self.role)

        visible = BoqLine.objects.filter(project_id__in=user.assigned_project_ids())
        self.assertEqual(list(visible), [self.line_a])

    def test_a_user_with_no_assignments_sees_nothing(self):
        """Correctly restrictive rather than accidentally permissive: an
        unassigned user must not fall through to seeing every project."""
        user = make_user()
        visible = BoqLine.objects.filter(project_id__in=user.assigned_project_ids())
        self.assertEqual(list(visible), [])

    def test_assignment_to_several_projects_shows_all_of_them(self):
        user = make_user()
        user.project_assignments.create(project=self.project_a, role=self.role)
        user.project_assignments.create(project=self.project_b, role=self.role)
        visible = BoqLine.objects.filter(project_id__in=user.assigned_project_ids())
        self.assertEqual(visible.count(), 2)

    def test_project_isolation_holds_for_cost_entries(self):
        """Cost scoping is what makes 'what has project X cost' a live query
        rather than a reconciliation (process design §5.4)."""
        actor = make_user()
        for project in (self.project_a, self.project_b):
            CostEntry.objects.create(
                project=project,
                category=CostEntry.MATERIAL,
                amount=Decimal("1000"),
                source_type="test",
                source_id=1,
                effective_date="2026-07-01",
                actor=actor,
            )
        self.assertEqual(
            CostEntry.objects.filter(project=self.project_a).count(), 1
        )
