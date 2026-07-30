"""Capability gating on views and actions.

Hiding a nav item is presentation. These tests check the thing that actually
matters: that typing the URL, or posting the form, is refused for a user whose
roles do not grant it.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import Client as HttpClient
from django.test import TestCase
from django.urls import reverse

from apps.accounts.capabilities import ALL_CODES, DEFAULT_ROLES
from apps.accounts.models import Role
from apps.core.models import Project
from apps.sales.models import Client, Lot, LotKind, Order

from .factories import make_user

PASSWORD = "test-pass-12345"


def user_with(*capabilities, is_administrator=False):
    user = make_user(is_administrator=is_administrator)
    user.set_password(PASSWORD)
    user.save()
    if capabilities:
        role = Role.objects.create(
            code=f"role{user.pk}", name="Test role", capabilities=list(capabilities)
        )
        user.user_roles.create(role=role)
    return user


def signed_in(user):
    http = HttpClient()
    http.login(username=user.username, password=PASSWORD)
    return http


class CapabilityCatalogueTests(TestCase):
    def test_default_roles_only_grant_capabilities_that_exist(self):
        """A typo in a default role would silently grant nothing."""
        for code, _, caps in DEFAULT_ROLES:
            unknown = set(caps) - ALL_CODES
            self.assertEqual(unknown, set(), f"role {code} references unknown {unknown}")

    def test_the_administrator_role_covers_the_whole_catalogue(self):
        caps = dict((c, caps) for c, _, caps in DEFAULT_ROLES)["administrator"]
        self.assertEqual(set(caps), ALL_CODES)


class ViewGateTests(TestCase):
    """Each screen refuses a user without its view capability."""

    SCREENS = [
        ("dashboard", "dashboard:view"),
        ("crm", "crm:view"),
        ("orders", "sales:view"),
        ("projects", "projects:view"),
        ("boq_list", "boq:view"),
        ("admin_home", "admin:manage"),
    ]

    def test_a_user_with_no_capabilities_is_refused_every_screen(self):
        http = signed_in(user_with())
        for name, _ in self.SCREENS:
            self.assertEqual(
                http.get(reverse(name)).status_code, 403, f"{name} should be refused"
            )

    def test_each_capability_opens_only_its_own_screen(self):
        for name, capability in self.SCREENS:
            http = signed_in(user_with(capability))
            self.assertEqual(http.get(reverse(name)).status_code, 200, name)
            for other, _ in self.SCREENS:
                if other == name:
                    continue
                self.assertEqual(
                    http.get(reverse(other)).status_code, 403,
                    f"{capability} should not open {other}",
                )

    def test_an_administrator_opens_everything(self):
        http = signed_in(user_with(is_administrator=True))
        for name, _ in self.SCREENS:
            self.assertEqual(http.get(reverse(name)).status_code, 200, name)

    def test_an_anonymous_visitor_is_sent_to_login_not_refused(self):
        response = HttpClient().get(reverse("projects"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_the_refusal_names_the_missing_capability(self):
        http = signed_in(user_with("crm:view"))
        response = http.get(reverse("boq_list"))
        self.assertContains(response, "boq:view", status_code=403)


class NavigationTests(TestCase):
    def test_the_nav_shows_only_what_the_user_can_open(self):
        http = signed_in(user_with("dashboard:view", "crm:view"))
        body = http.get(reverse("dashboard")).content.decode()
        self.assertIn(reverse("crm"), body)
        self.assertNotIn(reverse("boq_list"), body)
        self.assertNotIn(reverse("admin_home"), body)


class ActionGateTests(TestCase):
    """Seeing a screen is not permission to act on it."""

    def setUp(self):
        client = Client.objects.create(name="Test client")
        self.order = Order.objects.create(client=client, number="SO-ACC-1")
        Lot.objects.create(
            order=self.order, name="Lot 1", kind=LotKind.ITEMIZED, price=Decimal("100000")
        )

    def test_viewing_sales_does_not_allow_confirming_an_order(self):
        http = signed_in(user_with("sales:view"))
        response = http.post(
            reverse("order_detail", args=[self.order.pk]),
            {"action": "confirm", "committed_delivery_date": "2027-06-30"},
            follow=True,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.DRAFT)
        self.assertContains(response, "do not have permission")

    def test_the_confirm_capability_allows_it(self):
        http = signed_in(user_with("sales:view", "order:confirm"))
        http.post(
            reverse("order_detail", args=[self.order.pk]),
            {"action": "confirm", "committed_delivery_date": "2027-06-30"},
            follow=True,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.HELD)

    def test_confirming_does_not_also_allow_approving_kickoff(self):
        """Two separate permissions, deliberately — one person preparing the
        order should not be able to wave it through on their own."""
        http = signed_in(user_with("sales:view", "order:confirm"))
        http.post(
            reverse("order_detail", args=[self.order.pk]),
            {"action": "confirm", "committed_delivery_date": "2027-06-30"},
            follow=True,
        )
        response = http.post(
            reverse("order_detail", args=[self.order.pk]), {"action": "kickoff"}, follow=True
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.HELD)
        self.assertEqual(Project.objects.count(), 0)
        self.assertContains(response, "do not have permission")

    def test_planning_a_phase_needs_its_own_capability(self):
        project = Project.objects.create(
            code="acc-1", name="Access test", effective_committed_date=date.today() + timedelta(days=90)
        )
        http = signed_in(user_with("projects:view"))
        response = http.post(
            reverse("project_detail", args=[project.pk]),
            {
                "action": "plan_phase",
                "name": "Site visit",
                "kind": "site_visit",
                "planned_end": str(date.today() + timedelta(days=30)),
                "sequence": 1,
            },
            follow=True,
        )
        self.assertEqual(project.schedule_phases.count(), 0)
        self.assertContains(response, "do not have permission")


class AdminScreenTests(TestCase):
    def setUp(self):
        self.admin = user_with(is_administrator=True)
        self.http = signed_in(self.admin)

    def test_a_role_can_be_created_and_given_permissions(self):
        self.http.post(reverse("role_list"), {"name": "Store Keeper", "code": "store_keeper"})
        role = Role.objects.get(code="store_keeper")
        self.assertEqual(role.capabilities, [])

        self.http.post(
            reverse("role_detail", args=[role.pk]),
            {"name": "Store Keeper", "capabilities": ["projects:view", "boq:view"]},
        )
        role.refresh_from_db()
        self.assertEqual(role.capabilities, ["boq:view", "projects:view"])

    def test_unknown_capabilities_are_rejected_not_stored(self):
        role = Role.objects.create(code="r1", name="R1", capabilities=[])
        response = self.http.post(
            reverse("role_detail", args=[role.pk]),
            {"name": "R1", "capabilities": ["boq:view", "not:a:real:capability"]},
            follow=True,
        )
        role.refresh_from_db()
        self.assertEqual(role.capabilities, ["boq:view"])
        self.assertContains(response, "Ignored unknown capabilities")

    def test_granting_a_role_changes_what_the_user_can_open(self):
        person = user_with()
        http = signed_in(person)
        self.assertEqual(http.get(reverse("boq_list")).status_code, 403)

        role = Role.objects.create(code="boqview", name="BOQ viewer", capabilities=["boq:view"])
        self.http.post(reverse("user_detail", args=[person.pk]),
                       {"action": "roles", "roles": [str(role.pk)]})

        self.assertEqual(http.get(reverse("boq_list")).status_code, 200)

    def test_removing_a_role_revokes_access_immediately(self):
        role = Role.objects.create(code="boqview2", name="BOQ viewer", capabilities=["boq:view"])
        person = user_with()
        person.user_roles.create(role=role)
        http = signed_in(person)
        self.assertEqual(http.get(reverse("boq_list")).status_code, 200)

        self.http.post(reverse("user_detail", args=[person.pk]), {"action": "roles", "roles": []})
        self.assertEqual(http.get(reverse("boq_list")).status_code, 403)

    def test_an_administrator_cannot_remove_their_own_admin_rights(self):
        """It would lock them out of the only screen that could restore them."""
        response = self.http.post(
            reverse("user_detail", args=[self.admin.pk]), {"action": "toggle_admin"}, follow=True
        )
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_administrator)
        self.assertContains(response, "cannot remove your own Administrator rights")

    def test_an_administrator_cannot_deactivate_their_own_account(self):
        response = self.http.post(
            reverse("user_detail", args=[self.admin.pk]), {"action": "toggle_active"}, follow=True
        )
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertContains(response, "cannot deactivate your own account")

    def test_a_role_still_in_use_cannot_be_deleted(self):
        role = Role.objects.create(code="inuse", name="In use", capabilities=[])
        holder = user_with()
        holder.user_roles.create(role=role)

        response = self.http.post(
            reverse("role_detail", args=[role.pk]), {"action": "delete"}, follow=True
        )
        self.assertTrue(Role.objects.filter(pk=role.pk).exists())
        self.assertContains(response, "still held by")

    def test_role_changes_are_audited(self):
        from apps.platform_core.models import AuditEntry

        role = Role.objects.create(code="audited", name="Audited", capabilities=[])
        self.http.post(
            reverse("role_detail", args=[role.pk]),
            {"name": "Audited", "capabilities": ["boq:view"]},
        )
        entry = AuditEntry.objects.filter(
            entity_type="Role", entity_id=role.pk, action="role.permissions"
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.after["capabilities"], ["boq:view"])

    def test_a_non_administrator_cannot_reach_the_admin_screens(self):
        http = signed_in(user_with("projects:view", "boq:view"))
        for name, args in [
            ("admin_home", []),
            ("role_list", []),
            ("user_list", []),
        ]:
            self.assertEqual(http.get(reverse(name, args=args)).status_code, 403, name)


class CeoApprovalRoutingTests(TestCase):
    """Discern's instruction: at this stage every final sign-off routes to the CEO.

    "Final" means the last signature before something is committed, ordered,
    released or paid. Preparing work stays with the people who do it — only the
    approval moved.
    """

    def _role(self, code):
        return dict((c, caps) for c, _, caps in DEFAULT_ROLES)[code]

    def test_the_ceo_holds_every_final_approval(self):
        from apps.accounts.capabilities import FINAL_APPROVALS

        ceo = set(self._role("ceo"))
        self.assertEqual(set(FINAL_APPROVALS) - ceo, set())

    def test_no_other_role_holds_a_final_approval(self):
        """Otherwise the routing would be advisory rather than real."""
        from apps.accounts.capabilities import FINAL_APPROVALS

        for code, _, caps in DEFAULT_ROLES:
            if code in ("ceo", "administrator"):
                continue
            leaked = set(caps) & set(FINAL_APPROVALS)
            self.assertEqual(leaked, set(), f"{code} still holds {leaked}")

    def test_the_roles_that_prepare_the_work_keep_their_own_capabilities(self):
        """Centralising approval must not stop anyone doing their job."""
        self.assertIn("order:confirm", self._role("sales_manager"))
        self.assertIn("boq:sign_off", self._role("design_manager"))
        self.assertIn("procurement:award", self._role("purchase_manager"))
        self.assertIn("project:plan_schedule", self._role("project_manager"))
        self.assertIn("receipt:verify", self._role("site_engineer"))

    def test_the_ceo_can_also_hold_administrator(self):
        """Discern's choice. Roles compose, so this needs no special case."""
        ceo = user_with(*self._role("ceo"), is_administrator=True)
        self.assertTrue(ceo.has_capability("boq_revision:release"))
        self.assertTrue(ceo.has_capability("admin:manage"))

    def test_a_project_manager_can_no_longer_release_a_boq(self):
        http = signed_in(user_with(*self._role("project_manager")))
        self.assertEqual(http.get(reverse("boq_list")).status_code, 200)
        person = user_with(*self._role("project_manager"))
        self.assertFalse(person.has_capability("boq_revision:release"))

    def test_a_purchase_manager_still_awards_but_no_longer_approves(self):
        """The award was always theirs and stays theirs; the second signature
        above the value threshold is now the CEO's."""
        person = user_with(*self._role("purchase_manager"))
        self.assertTrue(person.has_capability("procurement:award"))
        self.assertFalse(person.has_capability("purchase_order:approve"))
