"""Phase 5: dashboards and the mobile site screens.

Two claims are worth defending. Every dashboard figure is a query over the
ledgers, so no two screens can disagree — the Construction Manager's expense
sheet and the Project Manager's are literally the same call. And the mobile
screens go through the same domain services as everything else, so a Store
Keeper on a phone at a site gate cannot post cost the desktop would refuse.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import Client as HttpClient
from django.test import TestCase
from django.urls import reverse

from apps.core.models import BoqLine, Item, Location
from apps.finance import services as finance
from apps.finance.models import ExpenseCategory, SiteExpense
from apps.inventory import services as inventory
from apps.inventory.models import ExpectedReceipt, GoodsReceipt
from apps.platform_core.models import AdminOverride, CostEntry, Override
from apps.platform_core.services import costing, events
from apps.platform_core.services.stock import post_move
from apps.procurement import services as procurement
from apps.procurement.models import Vendor, VendorRate
from apps.projects.models import SchedulePhase
from apps.reporting import services as reporting
from apps.subcontracts import services as subs
from apps.testsupport.models import DemoDocument

from .factories import make_item, make_project, make_role, make_user

PASSWORD = "test-pass-12345"


def actor_with(*caps, is_administrator=False):
    user = make_user(is_administrator=is_administrator)
    user.set_password(PASSWORD)
    user.save()
    if caps:
        user.user_roles.create(role=make_role(capabilities=list(caps)))
    return user


def signed_in(user):
    http = HttpClient()
    http.login(username=user.username, password=PASSWORD)
    return http


class DashboardDataTestCase(TestCase):
    def setUp(self):
        self.project = make_project(code="dash")
        self.project.effective_committed_date = date.today() + timedelta(days=120)
        self.project.save()
        self.site = Location.objects.create(
            code="dash-site", name="Site", kind=Location.SITE, project=self.project
        )
        self.actor = actor_with(
            "service_order:issue", "service_order:certify", "expenses:approve",
            "expenses:submit", "purchase_order:approve",
        )


class ProjectDashboardTests(DashboardDataTestCase):
    def test_an_empty_project_reports_nothing_rather_than_failing(self):
        data = reporting.project_dashboard(self.project)
        self.assertIsNone(data["boq"]["revision_number"])
        self.assertIsNone(data["progress"]["percent"])
        self.assertEqual(data["profit"]["margin"], Decimal("0"))

    def test_ordered_received_and_billed_are_three_different_numbers(self):
        """A single 'spend' figure hides which of the three you are looking at."""
        movement = reporting.purchase_movement(self.project)
        self.assertEqual(movement["ordered"], Decimal("0"))
        self.assertEqual(movement["received"], Decimal("0"))
        self.assertEqual(movement["billed"], Decimal("0"))

    def test_schedule_headroom_is_days_between_the_last_phase_and_the_promise(self):
        SchedulePhase.objects.create(
            project=self.project, name="Construction", kind=SchedulePhase.CONSTRUCTION,
            planned_end=date.today() + timedelta(days=90),
        )
        status = reporting.schedule_status(self.project)
        self.assertEqual(status["headroom_days"], 30)
        self.assertEqual(status["overdue"], [])

    def test_an_overdue_phase_is_surfaced(self):
        SchedulePhase.objects.create(
            project=self.project, name="Site visit", kind=SchedulePhase.SITE_VISIT,
            planned_end=date.today() - timedelta(days=5),
        )
        self.assertEqual(len(reporting.schedule_status(self.project)["overdue"]), 1)

    def test_site_progress_reports_certified_not_merely_reported(self):
        """Reporting progress releases no money, so it must not move this figure."""
        trade = Item.objects.create(code="civ", name="Civil", uom="sqm", item_type=Item.SERVICE)
        line = BoqLine.objects.create(
            project=self.project, item=trade, description="Civil work",
            quantity=Decimal("100"), uom="sqm", route=BoqLine.SERVICE,
        )
        vendor = Vendor.objects.create(name="Civil Co", is_empanelled=True)
        VendorRate.objects.create(vendor=vendor, item=trade, rate=Decimal("1000"), uom="sqm")
        order = subs.create_service_order(
            boq_line=line, vendor=vendor, quantity=100, actor=self.actor
        )
        subs.issue(order=order, actor=self.actor, threshold=Decimal("99999999"))

        reporter = actor_with("service_order:progress")
        subs.log_progress(order=order, percent=80, actor=reporter)
        self.assertEqual(reporting.site_progress(self.project)["percent"], Decimal("0"))

        subs.certify(order=order, quantity=25, actor=self.actor)
        self.assertEqual(reporting.site_progress(self.project)["percent"], Decimal("25"))


class ExpenseSheetTests(DashboardDataTestCase):
    def _expense(self, amount="10000", approve=True):
        expense = finance.submit_expense(
            project=self.project, category=ExpenseCategory.ROOM_RENT,
            amount=Decimal(amount), expense_date=date.today(), actor=self.actor,
        )
        if approve:
            finance.approve_expense(expense=expense, actor=self.actor)
        return expense

    def test_unapproved_claims_are_shown_not_hidden(self):
        """Hiding real spend until approval flatters the margin exactly when it
        matters."""
        self._expense(approve=False)
        sheet = reporting.expense_vs_income(self.project)
        self.assertEqual(sheet["pending_total"], Decimal("10000"))
        self.assertEqual(sheet["expense_total"], Decimal("0"))

    def test_approved_claims_move_from_pending_into_the_total(self):
        expense = self._expense(approve=False)
        finance.approve_expense(expense=expense, actor=self.actor)
        sheet = reporting.expense_vs_income(self.project)
        self.assertEqual(sheet["pending_total"], Decimal("0"))
        self.assertEqual(sheet["expense_total"], Decimal("10000"))

    def test_the_construction_manager_and_project_manager_see_one_set_of_numbers(self):
        """Not two reports that could drift — the same call."""
        self._expense()
        cm_view = reporting.expense_vs_income(self.project)
        pm_view = reporting.expense_vs_income(self.project)
        self.assertEqual(cm_view["expense_total"], pm_view["expense_total"])
        self.assertEqual(cm_view["revenue_total"], pm_view["revenue_total"])

    def test_the_breakdown_groups_by_category(self):
        self._expense(amount="5000")
        finance.approve_expense(
            expense=finance.submit_expense(
                project=self.project, category=ExpenseCategory.WATER,
                amount=Decimal("2000"), expense_date=date.today(), actor=self.actor,
            ),
            actor=self.actor,
        )
        rows = {r["category"]: r["total"] for r in reporting.expense_breakdown(self.project)}
        self.assertEqual(rows["Room rent"], Decimal("5000"))
        self.assertEqual(rows["Water"], Decimal("2000"))


class WarehouseStockTests(DashboardDataTestCase):
    def test_stock_is_reported_across_every_location_not_one_project(self):
        """Keeping stock efficient across the company is the Purchase Manager's
        job, so this view is deliberately not project-scoped."""
        other = make_project(code="dash2")
        other_site = Location.objects.create(
            code="dash2-site", name="Other", kind=Location.SITE, project=other
        )
        item = make_item()
        for location, qty in ((self.site, "40"), (other_site, "25")):
            post_move(
                item_id=item.pk, quantity=Decimal(qty), to_location_id=location.pk,
                unit_value=Decimal("100"), source_type="test", source_id=1, actor=self.actor,
            )
        rows = reporting.warehouse_stock(item_id=item.pk)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["on_hand"] for r in rows), Decimal("65"))
        self.assertEqual(sum(r["value"] for r in rows), Decimal("6500"))

    def test_a_location_drawn_down_to_zero_drops_out(self):
        item = make_item()
        post_move(
            item_id=item.pk, quantity=Decimal("10"), to_location_id=self.site.pk,
            unit_value=Decimal("50"), source_type="test", source_id=1, actor=self.actor,
        )
        post_move(
            item_id=item.pk, quantity=Decimal("10"), from_location_id=self.site.pk,
            source_type="test", source_id=2, actor=self.actor,
        )
        self.assertEqual(reporting.warehouse_stock(item_id=item.pk), [])


class PortfolioTests(DashboardDataTestCase):
    def test_the_worst_margin_sorts_first(self):
        """The project needing attention should not be buried at the bottom."""
        weak = make_project(code="weak")
        for project, revenue, cost in (
            (self.project, "1000000", "700000"), (weak, "500000", "495000")
        ):
            costing.post_cost(
                project_id=project.pk, category=CostEntry.REVENUE, amount=Decimal(revenue),
                source_type="test", source_id=1, actor=self.actor,
            )
            costing.post_cost(
                project_id=project.pk, category=CostEntry.MATERIAL, amount=Decimal(cost),
                source_type="test", source_id=2, actor=self.actor,
            )
        rows = reporting.portfolio()["rows"]
        self.assertEqual(rows[0]["project"], weak)

    def test_overrides_are_surfaced_for_director_review(self):
        """Permitted, but never quiet."""
        admin = actor_with(is_administrator=True)
        doc = DemoDocument.objects.create(name="d", value=Decimal("1"))
        doc.lock(self.actor)
        doc.name = "corrected"
        doc.save(override=Override(admin, reason="agreed with the PM"))

        overrides = reporting.portfolio()["overrides"]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].reason, "agreed with the PM")

    def test_a_negative_margin_project_is_flagged_at_risk(self):
        costing.post_cost(
            project_id=self.project.pk, category=CostEntry.MATERIAL, amount=Decimal("50000"),
            source_type="test", source_id=1, actor=self.actor,
        )
        self.assertEqual(len(reporting.portfolio()["at_risk"]), 1)


class DashboardScreenTests(DashboardDataTestCase):
    def test_each_dashboard_needs_its_own_capability(self):
        nobody = signed_in(actor_with())
        for name, args in (
            ("portfolio", []),
            ("pm_dashboard", [self.project.pk]),
            ("purchase_dashboard", []),
            ("expense_sheet", [self.project.pk]),
        ):
            self.assertEqual(nobody.get(reverse(name, args=args)).status_code, 403, name)

    def test_the_dashboards_render_for_a_user_who_holds_them(self):
        http = signed_in(actor_with(
            "dashboard:view", "projects:view", "procurement:view", "expenses:view"
        ))
        for name, args in (
            ("portfolio", []),
            ("pm_dashboard", [self.project.pk]),
            ("purchase_dashboard", []),
            ("expense_sheet", [self.project.pk]),
        ):
            self.assertEqual(http.get(reverse(name, args=args)).status_code, 200, name)


class MobileScreenTests(TestCase):
    """The highest-volume, lowest-patience interactions in the whole system.

    If these are painful on a phone at a site gate the ledgers stay empty and
    every dashboard lies, so they get the same enforcement and their own tests.
    """

    def setUp(self):
        self.project = make_project(code="mob")
        self.site = Location.objects.create(
            code="mob-site", name="Site", kind=Location.SITE, project=self.project
        )
        self.pm = actor_with("boq_revision:release")
        self.buyer = actor_with(
            "procurement:rfq", "procurement:award", "purchase_order:create",
            "purchase_order:approve",
        )
        self.store = actor_with("receipt:view", "receipt:record")
        self.engineer = actor_with("receipt:view", "receipt:verify")

        item = Item.objects.create(code="mob-item", name="MS pipe", uom="Mtr")
        boq_line = BoqLine.objects.create(
            project=self.project, item=item, description="MS pipe 100mm",
            quantity=Decimal("50"), uom="Mtr", route=BoqLine.SUPPLY,
        )
        vendor = Vendor.objects.create(name="Pipe Co")
        from apps.procurement.models import ProcurementRequest, ProcurementRequestLine, RequestSource

        request = ProcurementRequest.objects.create(
            project=self.project, source=RequestSource.BOQ_RELEASE, number="PR-MOB-1",
            status=ProcurementRequest.APPROVED, requested_by=self.pm,
        )
        line = ProcurementRequestLine.objects.create(
            request=request, boq_line=boq_line, item=item,
            description="MS pipe 100mm", quantity=Decimal("50"), uom="Mtr",
        )
        rfq = procurement.create_rfq(request=request, vendors=[vendor], actor=self.buyer)
        rfq.min_vendors_waived_reason = "sole supplier"
        rfq.save(update_fields=["min_vendors_waived_reason"])
        procurement.issue_rfq(rfq=rfq, actor=self.buyer)
        procurement.record_quote(
            rfq_vendor=rfq.vendors.first(),
            quotes=[{"request_line": line, "rate": "500"}], actor=self.buyer,
        )
        award = procurement.award_line(
            rfq=rfq, request_line=line, vendor=vendor, actor=self.buyer
        )
        self.order = procurement.create_purchase_order(awards=[award], actor=self.buyer)
        procurement.submit_purchase_order(order=self.order, actor=self.buyer)
        events.drain()
        self.expected = ExpectedReceipt.objects.get()

    def test_the_home_screen_shows_only_what_this_role_can_do(self):
        store_view = signed_in(self.store).get(reverse("m_home")).content.decode()
        self.assertIn("Material arriving", store_view)
        self.assertNotIn("To verify", store_view)

        engineer_view = signed_in(self.engineer).get(reverse("m_home")).content.decode()
        self.assertIn("To verify", engineer_view)

    def test_a_store_keeper_records_an_arrival_from_the_phone(self):
        http = signed_in(self.store)
        http.post(reverse("m_receive", args=[self.expected.pk]),
                  {"quantity": "50", "challan": "CH-99"}, follow=True)
        receipt = GoodsReceipt.objects.get()
        self.assertEqual(receipt.received_qty, Decimal("50"))
        self.assertEqual(receipt.vendor_challan, "CH-99")

    def test_recording_on_the_phone_still_posts_no_cost(self):
        """The same rule as the desktop, because it is the same service."""
        signed_in(self.store).post(
            reverse("m_receive", args=[self.expected.pk]), {"quantity": "50"}, follow=True
        )
        self.assertEqual(costing.project_total(self.project.pk), Decimal("0"))

    def test_a_store_keeper_cannot_verify_their_own_delivery_on_the_phone(self):
        http = signed_in(self.store)
        http.post(reverse("m_receive", args=[self.expected.pk]), {"quantity": "50"}, follow=True)
        receipt = GoodsReceipt.objects.get()

        response = http.post(
            reverse("m_verify", args=[receipt.pk]), {"accepted": "50"}, follow=True
        )
        self.assertContains(response, "do not have permission")
        self.assertEqual(costing.project_total(self.project.pk), Decimal("0"))

    def test_the_site_engineer_verifies_and_cost_posts(self):
        signed_in(self.store).post(
            reverse("m_receive", args=[self.expected.pk]), {"quantity": "50"}, follow=True
        )
        receipt = GoodsReceipt.objects.get()
        signed_in(self.engineer).post(
            reverse("m_verify", args=[receipt.pk]),
            {"accepted": "48", "rejected": "2", "notes": "2 dented"}, follow=True,
        )
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.MATERIAL), Decimal("24000")
        )

    def test_the_mobile_screens_require_sign_in(self):
        response = HttpClient().get(reverse("m_home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_a_bad_quantity_is_reported_rather_than_crashing(self):
        http = signed_in(self.store)
        response = http.post(
            reverse("m_receive", args=[self.expected.pk]), {"quantity": "abc"}, follow=True
        )
        self.assertContains(response, "must be a number")
        self.assertEqual(GoodsReceipt.objects.count(), 0)
