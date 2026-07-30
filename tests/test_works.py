"""Phase 4: fabrication, subcontracts, site expenses, dead-stock redeployment.

The three routes off a released BOQ now all exist. What is defended here:
fabrication capping the finished item but deliberately *not* its raw materials;
service orders skipping the RFQ only for empanelled vendors on agreed rates;
certification as a distinct act from reporting progress; site expenses reaching
the same margin figure as everything else; and inter-project transfers moving
value explicitly rather than stock moving silently.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import BoqLine, Item, Location
from apps.fabrication import services as fab
from apps.fabrication.models import (
    BillOfMaterials,
    BomComponent,
    FabricationMode,
    FabricationOrder,
)
from apps.finance import services as finance
from apps.finance.models import ExpenseCategory, SiteExpense
from apps.inventory import services as inventory
from apps.inventory.models import ExcessStockFlag, StockTransfer
from apps.platform_core.exceptions import CeilingExceeded, DomainError
from apps.platform_core.models import CostEntry, Notification
from apps.platform_core.services import costing, events
from apps.platform_core.services.ceiling import headroom
from apps.platform_core.services.stock import on_hand, post_move
from apps.procurement.models import ProcurementRequest, RequestSource, Vendor, VendorRate
from apps.subcontracts import services as subs
from apps.subcontracts.models import ServiceOrder, VendorBill

from .factories import make_item, make_project, make_role, make_user


def actor_with(*caps):
    user = make_user()
    user.user_roles.create(role=make_role(capabilities=list(caps)))
    return user


class WorksTestCase(TestCase):
    def setUp(self):
        self.project = make_project(code="works")
        self.site = Location.objects.create(
            code="works-site", name="Site", kind=Location.SITE, project=self.project
        )
        self.designer = actor_with("fabrication:manage")
        self.cm = actor_with(
            "service_order:issue", "service_order:progress", "service_order:certify",
            "expenses:approve",
        )
        self.engineer = actor_with(
            "service_order:progress", "stock:flag_excess", "expenses:submit"
        )
        self.purchase_mgr = actor_with("stock:transfer", "purchase_order:approve")


class FabricationTests(WorksTestCase):
    def setUp(self):
        super().setUp()
        self.staircase = Item.objects.create(code="ms-stair", name="MS staircase", uom="nos")
        self.plate = Item.objects.create(code="ms-plate", name="MS plate 10mm", uom="kg")
        self.angle = Item.objects.create(code="ms-angle", name="MS angle 50x50", uom="kg")

        self.bom = BillOfMaterials.objects.create(item=self.staircase, name="Staircase")
        BomComponent.objects.create(bom=self.bom, item=self.plate, quantity=Decimal("120"), uom="kg")
        BomComponent.objects.create(
            bom=self.bom, item=self.angle, quantity=Decimal("80"), uom="kg",
            wastage_pct=Decimal("5"),
        )
        self.boq_line = BoqLine.objects.create(
            project=self.project, item=self.staircase, description="Custom MS staircase",
            quantity=Decimal("2"), uom="nos", route=BoqLine.FABRICATE,
        )

    def test_a_fabrication_order_consumes_the_boq_ceiling(self):
        self.assertEqual(headroom(self.boq_line.pk), Decimal("2"))
        fab.create_order(boq_line=self.boq_line, quantity=2, actor=self.designer)
        self.assertEqual(headroom(self.boq_line.pk), Decimal("0"))

    def test_it_cannot_exceed_the_boq(self):
        with self.assertRaises(CeilingExceeded):
            fab.create_order(boq_line=self.boq_line, quantity=3, actor=self.designer)

    def test_only_a_fabricate_line_can_be_fabricated(self):
        supply = BoqLine.objects.create(
            project=self.project, item=self.plate, description="Cement",
            quantity=Decimal("10"), uom="bag", route=BoqLine.SUPPLY,
        )
        with self.assertRaises(DomainError) as ctx:
            fab.create_order(boq_line=supply, quantity=1, actor=self.designer)
        self.assertIn("routed SUPPLY", str(ctx.exception))

    def test_the_recipe_explodes_with_its_wastage_allowance(self):
        order = fab.create_order(boq_line=self.boq_line, quantity=2, actor=self.designer)
        planned = {c.item_id: c.planned_qty for c in order.consumption.all()}
        self.assertEqual(planned[self.plate.pk], Decimal("240"))
        self.assertEqual(planned[self.angle.pk], Decimal("168"))  # 160 + 5%

    def test_a_shortfall_raises_child_procurement_requests(self):
        order = fab.create_order(boq_line=self.boq_line, quantity=2, actor=self.designer)
        request = fab.request_shortfall(order=order, actor=self.designer)

        self.assertEqual(request.source, RequestSource.FABRICATION_SHORTFALL)
        self.assertEqual(request.lines.count(), 2)
        order.refresh_from_db()
        self.assertEqual(order.status, FabricationOrder.AWAITING_MATERIAL)

    def test_raw_materials_are_deliberately_not_ceiling_checked(self):
        """They are components consumed to produce the line, not the line's own
        item. The finished quantity was already capped upstream."""
        order = fab.create_order(boq_line=self.boq_line, quantity=2, actor=self.designer)
        request = fab.request_shortfall(order=order, actor=self.designer)
        for line in request.lines.all():
            self.assertIsNone(line.boq_line)

    def test_production_cannot_start_while_material_is_short(self):
        order = fab.create_order(boq_line=self.boq_line, quantity=2, actor=self.designer)
        with self.assertRaises(DomainError) as ctx:
            fab.start(order=order, actor=self.designer)
        self.assertIn("short of raw material", str(ctx.exception))

    def _stock_the_works(self, order):
        works = Location.objects.get(code=f"{self.project.code}-WORKS")
        for item, qty in ((self.plate, "240"), (self.angle, "168")):
            post_move(
                item_id=item.pk, quantity=Decimal(qty), to_location_id=works.pk,
                source_type="test", source_id=1, actor=self.designer,
            )
        return works

    def test_completion_produces_the_item_and_posts_fabrication_cost(self):
        order = fab.create_order(boq_line=self.boq_line, quantity=2, actor=self.designer)
        works = self._stock_the_works(order)
        fab.start(order=order, actor=self.designer)
        fab.complete(order=order, actor=self.designer, unit_cost=Decimal("41000"))

        self.assertEqual(on_hand(self.staircase.pk, self.site.pk), Decimal("2"))
        self.assertEqual(on_hand(self.plate.pk, works.pk), Decimal("0"))
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.FABRICATION), Decimal("82000")
        )

    def test_over_consumption_is_visible_rather_than_absorbed(self):
        """The compensating control for exempting raw materials from the ceiling."""
        order = fab.create_order(boq_line=self.boq_line, quantity=2, actor=self.designer)
        works = self._stock_the_works(order)
        post_move(
            item_id=self.plate.pk, quantity=Decimal("30"), to_location_id=works.pk,
            source_type="test", source_id=2, actor=self.designer,
        )
        fab.start(order=order, actor=self.designer)
        fab.complete(
            order=order, actor=self.designer, unit_cost=Decimal("41000"),
            actual_consumption={self.plate.pk: Decimal("265")},
        )
        line = order.consumption.get(item=self.plate)
        self.assertEqual(line.variance, Decimal("25"))
        self.assertTrue(line.is_over)

    def test_a_completed_order_is_locked(self):
        order = fab.create_order(boq_line=self.boq_line, quantity=2, actor=self.designer)
        self._stock_the_works(order)
        fab.start(order=order, actor=self.designer)
        fab.complete(order=order, actor=self.designer, unit_cost=Decimal("1"))
        order.refresh_from_db()
        self.assertTrue(order.is_locked)

    def test_job_work_needs_a_vendor(self):
        with self.assertRaises(DomainError):
            fab.create_order(
                boq_line=self.boq_line, quantity=1, actor=self.designer,
                mode=FabricationMode.JOB_WORK,
            )

    def test_job_work_issues_components_out_and_receives_the_item_back(self):
        """Decision #3: both modes supported. Job work moves components to the
        vendor's premises and the finished item comes back."""
        vendor = Vendor.objects.create(name="Howrah Fabricators")
        order = fab.create_order(
            boq_line=self.boq_line, quantity=2, actor=self.designer,
            mode=FabricationMode.JOB_WORK, vendor=vendor,
            job_work_charge=Decimal("18000"),
        )
        works = self._stock_the_works(order)
        fab.start(order=order, actor=self.designer)

        vendor_location = Location.objects.get(code=f"VENDOR-{vendor.pk}")
        self.assertEqual(on_hand(self.plate.pk, vendor_location.pk), Decimal("240"))
        self.assertEqual(on_hand(self.plate.pk, works.pk), Decimal("0"))

        fab.complete(order=order, actor=self.designer)
        self.assertEqual(on_hand(self.staircase.pk, self.site.pk), Decimal("2"))
        self.assertEqual(on_hand(self.plate.pk, vendor_location.pk), Decimal("0"))
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.FABRICATION), Decimal("18000")
        )


class ServiceOrderTests(WorksTestCase):
    def setUp(self):
        super().setUp()
        self.trade = Item.objects.create(
            code="plumbing", name="Plumbing installation", uom="sqm", item_type=Item.SERVICE
        )
        self.boq_line = BoqLine.objects.create(
            project=self.project, item=self.trade, description="Plumbing installation",
            quantity=Decimal("400"), uom="sqm", route=BoqLine.SERVICE,
        )
        self.empanelled = Vendor.objects.create(name="Bengal Plumbing", is_empanelled=True)
        VendorRate.objects.create(
            vendor=self.empanelled, item=self.trade, rate=Decimal("450"), uom="sqm"
        )
        self.newcomer = Vendor.objects.create(name="New Trades Co")

    def test_an_empanelled_vendor_on_an_agreed_rate_skips_the_rfq(self):
        order = subs.create_service_order(
            boq_line=self.boq_line, vendor=self.empanelled, quantity=400, actor=self.cm
        )
        self.assertEqual(order.rate, Decimal("450"))
        self.assertEqual(order.total_value, Decimal("180000"))

    def test_a_vendor_without_an_agreed_rate_is_routed_to_a_normal_rfq(self):
        """Exactly when price discovery has value."""
        with self.assertRaises(DomainError) as ctx:
            subs.create_service_order(
                boq_line=self.boq_line, vendor=self.newcomer, quantity=100, actor=self.cm
            )
        self.assertIn("normal RFQ", str(ctx.exception))

    def test_a_service_order_consumes_the_boq_ceiling(self):
        subs.create_service_order(
            boq_line=self.boq_line, vendor=self.empanelled, quantity=400, actor=self.cm
        )
        self.assertEqual(headroom(self.boq_line.pk), Decimal("0"))

    def test_only_a_service_line_can_be_subcontracted(self):
        supply = BoqLine.objects.create(
            project=self.project, item=self.trade, description="Cement",
            quantity=Decimal("10"), uom="bag", route=BoqLine.SUPPLY,
        )
        with self.assertRaises(DomainError):
            subs.create_service_order(
                boq_line=supply, vendor=self.empanelled, quantity=1, actor=self.cm
            )

    def test_a_direct_service_order_is_not_a_route_around_the_threshold(self):
        order = subs.create_service_order(
            boq_line=self.boq_line, vendor=self.empanelled, quantity=400, actor=self.cm
        )
        subs.issue(order=order, actor=self.cm, threshold=Decimal("1000"))
        order.refresh_from_db()
        self.assertEqual(order.status, ServiceOrder.AWAITING_APPROVAL)

        subs.approve(order=order, actor=self.purchase_mgr)
        order.refresh_from_db()
        self.assertEqual(order.status, ServiceOrder.ISSUED)


class CertificationTests(WorksTestCase):
    def setUp(self):
        super().setUp()
        trade = Item.objects.create(
            code="civil", name="Civil work", uom="sqm", item_type=Item.SERVICE
        )
        self.boq_line = BoqLine.objects.create(
            project=self.project, item=trade, description="Civil work",
            quantity=Decimal("100"), uom="sqm", route=BoqLine.SERVICE,
        )
        vendor = Vendor.objects.create(name="Site Civil Co", is_empanelled=True)
        VendorRate.objects.create(vendor=vendor, item=trade, rate=Decimal("1000"), uom="sqm")
        self.order = subs.create_service_order(
            boq_line=self.boq_line, vendor=vendor, quantity=100, actor=self.cm
        )
        subs.issue(order=self.order, actor=self.cm, threshold=Decimal("99999999"))

    def test_logging_progress_is_not_certifying(self):
        """Anyone with visibility may report; that releases no money."""
        subs.log_progress(order=self.order, percent=60, actor=self.engineer)
        self.assertEqual(self.order.certified_qty, Decimal("0"))
        self.assertEqual(costing.project_total(self.project.pk, CostEntry.SUBCONTRACT), Decimal("0"))

    def test_certification_raises_a_vendor_bill_and_posts_cost(self):
        subs.certify(order=self.order, quantity=40, actor=self.cm)
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.SUBCONTRACT), Decimal("40000")
        )
        bill = VendorBill.objects.get()
        self.assertEqual(bill.amount, Decimal("40000"))
        self.assertEqual(bill.source_type, "service_certification")

    def test_there_is_no_goods_receipt_behind_a_service_bill(self):
        """Nothing physical to receive."""
        from apps.inventory.models import GoodsReceipt

        subs.certify(order=self.order, quantity=40, actor=self.cm)
        self.assertEqual(GoodsReceipt.objects.count(), 0)

    def test_running_bills_number_sequentially_and_accumulate(self):
        """Progressive billing is the norm, not an edge case."""
        first = subs.certify(order=self.order, quantity=40, actor=self.cm)
        second = subs.certify(order=self.order, quantity=35, actor=self.cm)

        self.assertEqual(first.running_bill_number, 1)
        self.assertEqual(second.running_bill_number, 2)
        self.assertEqual(self.order.certified_qty, Decimal("75"))
        self.assertEqual(self.order.outstanding_qty, Decimal("25"))
        self.assertEqual(self.order.percent_certified, Decimal("75"))

    def test_certifying_beyond_the_ordered_scope_is_refused(self):
        subs.certify(order=self.order, quantity=90, actor=self.cm)
        with self.assertRaises(DomainError) as ctx:
            subs.certify(order=self.order, quantity=20, actor=self.cm)
        self.assertIn("remains uncertified", str(ctx.exception))

    def test_only_the_certifier_capability_may_certify(self):
        with self.assertRaises(DomainError):
            subs.certify(order=self.order, quantity=10, actor=self.engineer)

    def test_closing_final_short_releases_the_balance_to_the_boq(self):
        subs.certify(order=self.order, quantity=70, actor=self.cm, is_final=True)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, ServiceOrder.COMPLETE)
        self.assertEqual(headroom(self.boq_line.pk), Decimal("30"))


class SiteExpenseTests(WorksTestCase):
    def _submit(self, category=ExpenseCategory.ROOM_RENT, amount="48000"):
        return finance.submit_expense(
            project=self.project, category=category, amount=Decimal(amount),
            expense_date=date(2026, 11, 1), actor=self.engineer,
        )

    def test_a_submitted_expense_is_not_yet_cost(self):
        self._submit()
        self.assertEqual(costing.project_total(self.project.pk, CostEntry.SITE_EXPENSE), Decimal("0"))

    def test_approval_posts_it_to_the_same_ledger_as_everything_else(self):
        """The structural fix: site running costs cannot miss the margin."""
        expense = self._submit()
        finance.approve_expense(expense=expense, actor=self.cm)
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.SITE_EXPENSE), Decimal("48000")
        )

    def test_site_expenses_reduce_the_project_margin(self):
        costing.post_cost(
            project_id=self.project.pk, category=CostEntry.REVENUE, amount=Decimal("500000"),
            source_type="test", source_id=1, actor=self.cm,
        )
        before = costing.profitability(self.project.pk)["margin"]
        finance.approve_expense(expense=self._submit(), actor=self.cm)
        after = costing.profitability(self.project.pk)["margin"]
        self.assertEqual(before - after, Decimal("48000"))

    def test_submitting_and_approving_are_separate_permissions(self):
        expense = self._submit()
        with self.assertRaises(DomainError):
            finance.approve_expense(expense=expense, actor=self.engineer)

    def test_all_five_categories_are_available(self):
        for category in ExpenseCategory.values:
            finance.approve_expense(
                expense=self._submit(category=category, amount="1000"), actor=self.cm
            )
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.SITE_EXPENSE), Decimal("5000")
        )

    def test_pending_claims_still_appear_on_the_comparison_sheet(self):
        """Hiding real spend until approved makes the margin optimistic exactly
        when it matters."""
        self._submit()
        sheet = finance.expense_vs_income(self.project.pk)
        self.assertEqual(sheet["pending"].count(), 1)


class ExcessStockTests(WorksTestCase):
    def setUp(self):
        super().setUp()
        self.item = make_item()
        self.other_project = make_project(code="other")
        self.other_site = Location.objects.create(
            code="other-site", name="Other site", kind=Location.SITE, project=self.other_project
        )
        post_move(
            item_id=self.item.pk, quantity=Decimal("100"), to_location_id=self.site.pk,
            unit_value=Decimal("250"), source_type="test", source_id=1, actor=self.engineer,
        )

    def _flag(self, quantity="40"):
        return inventory.flag_excess(
            item=self.item, location=self.site, quantity=Decimal(quantity),
            actor=self.engineer, notes="scope reduced",
        )

    def test_a_site_engineer_can_flag_excess_stock(self):
        flag = self._flag()
        self.assertEqual(flag.status, ExcessStockFlag.OPEN)
        self.assertEqual(flag.reason, ExcessStockFlag.AVAILABLE)

    def test_more_cannot_be_flagged_than_is_on_hand(self):
        with self.assertRaises(DomainError):
            self._flag(quantity="500")

    def test_flagging_notifies_the_dashboards_that_can_act(self):
        self._flag()
        events.drain()
        self.assertTrue(
            Notification.objects.filter(event_name="StockFlaggedExcess").exists()
        )

    def test_flagged_stock_is_not_written_off(self):
        """Scrapping destroys the record; this relabels it as usable elsewhere."""
        self._flag()
        self.assertEqual(on_hand(self.item.pk, self.site.pk), Decimal("100"))

    def test_the_receiving_project_manager_must_accept(self):
        transfer = inventory.redeploy(
            flag=self._flag(), to_location=self.other_site, actor=self.purchase_mgr
        )
        self.assertEqual(transfer.status, StockTransfer.PENDING)
        self.assertEqual(on_hand(self.item.pk, self.other_site.pk), Decimal("0"))

    def test_acceptance_moves_stock_and_posts_paired_cost_entries(self):
        """The one deliberate breach of project isolation — which is why the
        value moves explicitly rather than the stock moving silently."""
        transfer = inventory.redeploy(
            flag=self._flag(), to_location=self.other_site, actor=self.purchase_mgr
        )
        inventory.accept_transfer(transfer=transfer, actor=self.purchase_mgr)

        self.assertEqual(on_hand(self.item.pk, self.site.pk), Decimal("60"))
        self.assertEqual(on_hand(self.item.pk, self.other_site.pk), Decimal("40"))

        # 40 × ₹250 original purchase cost — decision #4.
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.STOCK_OUT), Decimal("-10000")
        )
        self.assertEqual(
            costing.project_total(self.other_project.pk, CostEntry.STOCK_IN), Decimal("10000")
        )

    def test_the_two_projects_margins_move_in_opposite_directions(self):
        transfer = inventory.redeploy(
            flag=self._flag(), to_location=self.other_site, actor=self.purchase_mgr
        )
        inventory.accept_transfer(transfer=transfer, actor=self.purchase_mgr)
        self.assertEqual(costing.profitability(self.project.pk)["cost"], Decimal("-10000"))
        self.assertEqual(costing.profitability(self.other_project.pk)["cost"], Decimal("10000"))

    def test_declining_leaves_everything_where_it_was(self):
        transfer = inventory.redeploy(
            flag=self._flag(), to_location=self.other_site, actor=self.purchase_mgr
        )
        inventory.decline_transfer(transfer=transfer, actor=self.purchase_mgr, reason="not needed")
        self.assertEqual(on_hand(self.item.pk, self.site.pk), Decimal("100"))
        self.assertEqual(costing.project_total(self.project.pk), Decimal("0"))

    def test_flagging_needs_the_capability(self):
        with self.assertRaises(DomainError):
            inventory.flag_excess(
                item=self.item, location=self.site, quantity=Decimal("1"),
                actor=self.purchase_mgr,
            )

    def test_a_completed_transfer_closes_its_flag(self):
        flag = self._flag()
        transfer = inventory.redeploy(
            flag=flag, to_location=self.other_site, actor=self.purchase_mgr
        )
        inventory.accept_transfer(transfer=transfer, actor=self.purchase_mgr)
        flag.refresh_from_db()
        self.assertEqual(flag.status, ExcessStockFlag.TRANSFERRED)
