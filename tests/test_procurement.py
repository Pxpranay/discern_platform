"""Phase 3: procurement and receipt — the loop closing.

From a released BOQ revision through sourcing to material verified at site with
its cost in the ledger. The parts worth defending are the ones the process
design is emphatic about: three vendors quoted, an award that is entirely the
Purchase Manager's call, the BOQ ceiling holding on purchase orders, and
**nothing entering a project's cost on an unverified delivery**.
"""

from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.test import TestCase

from apps.core.models import Item, Location, Project
from apps.engineering import importers
from apps.engineering import services as boq_services
from apps.inventory import services as inventory
from apps.inventory.models import Discrepancy, ExpectedReceipt, GoodsReceipt
from apps.platform_core.exceptions import CeilingExceeded, DomainError
from apps.platform_core.models import CostEntry
from apps.platform_core.services import costing, events
from apps.platform_core.services.ceiling import committed_qty, headroom
from apps.platform_core.services.stock import on_hand
from apps.procurement import services as procurement
from apps.procurement.models import (
    Award,
    ProcurementRequest,
    PurchaseOrder,
    RequestSource,
    Rfq,
    Vendor,
)

from .factories import make_project, make_role, make_user

FIXTURES = Path(__file__).parent / "fixtures"
REV0 = FIXTURES / "BOQ_Linac_Bldg_Rev0.xlsx"
REV1 = FIXTURES / "BOQ_Linac_Bldg_Rev1.xlsx"
PIPE_200 = "MS ERW Pipe 200 mm NB X 6 mm thk"


def actor_with(*caps):
    user = make_user()
    user.user_roles.create(role=make_role(capabilities=list(caps)))
    return user


class ProcurementFlowTestCase(TestCase):
    """Shared setup: a project with Rev 0 released and vendors on file."""

    def setUp(self):
        self.pm = actor_with("boq_revision:release", "procurement:approve_request")
        self.buyer = actor_with("procurement:rfq", "purchase_order:create")
        self.manager = actor_with(
            "procurement:rfq", "procurement:award", "purchase_order:create",
            "purchase_order:approve",
        )
        self.store = actor_with("receipt:record")
        self.engineer = actor_with("receipt:verify", "receipt:return")

        self.project = make_project(code="proc")
        self.revision = importers.import_revision(
            path=REV0, project=self.project, revision_number=0,
            signed_off_by=self.pm,
        )
        with transaction.atomic():
            boq_services.release_revision(revision=self.revision, actor=self.pm)
        events.drain()

        self.vendors = [
            Vendor.objects.create(name=n)
            for n in ("Steel & Pipes Co", "Kolkata Tubes", "Eastern Metals", "Bengal Supply")
        ]
        self.request = ProcurementRequest.objects.filter(project=self.project).first()

    def _line(self, description=PIPE_200):
        return self.request.lines.get(description=description)

    def _rfq_with_quotes(self, rates=("1200", "1150", "1310"), line=None):
        line = line or self._line()
        rfq = procurement.create_rfq(
            request=self.request, vendors=self.vendors[: len(rates)], actor=self.buyer
        )
        procurement.issue_rfq(rfq=rfq, actor=self.buyer)
        for rfq_vendor, rate in zip(rfq.vendors.all(), rates):
            procurement.record_quote(
                rfq_vendor=rfq_vendor,
                quotes=[{"request_line": line, "rate": rate, "qty": line.quantity}],
                actor=self.buyer,
            )
        return rfq


class RequestFromReleaseTests(ProcurementFlowTestCase):
    def test_releasing_a_revision_raises_a_procurement_request(self):
        self.assertIsNotNone(self.request)
        self.assertEqual(self.request.source, RequestSource.BOQ_RELEASE)
        self.assertEqual(self.request.status, ProcurementRequest.APPROVED)

    def test_the_request_carries_every_line_needing_purchase(self):
        """Rev 0 is an opening BOQ, so all nine lines are new."""
        self.assertEqual(self.request.lines.count(), 9)

    def test_a_replayed_event_does_not_duplicate_the_request(self):
        events.drain()
        events.drain()
        self.assertEqual(
            ProcurementRequest.objects.filter(boq_revision=self.revision).count(), 1
        )

    def test_only_delta_outcomes_become_lines(self):
        """Rev 1 changes four lines; the other six must not reach Procurement."""
        revision1 = importers.import_revision(
            path=REV1, project=self.project, revision_number=1, signed_off_by=self.pm
        )
        with transaction.atomic():
            boq_services.release_revision(revision=revision1, actor=self.pm)
        events.drain()

        request = ProcurementRequest.objects.get(boq_revision=revision1)
        self.assertEqual(request.lines.count(), 3)
        self.assertEqual(
            request.lines.get(description=PIPE_200).quantity, Decimal("22")
        )


class SiteRequisitionTests(ProcurementFlowTestCase):
    def test_a_site_requisition_waits_for_the_project_manager(self):
        line = self.revision.sections.first().lines.first()
        request = procurement.raise_site_requisition(
            project=self.project, actor=self.engineer,
            lines=[{"boq_line": line, "description": line.description, "quantity": 2}],
        )
        self.assertEqual(request.status, ProcurementRequest.AWAITING_APPROVAL)
        self.assertTrue(request.is_site_raised)

    def test_a_site_requisition_cannot_exceed_the_boq_ceiling(self):
        """A shortcut to requisitioning sooner, not a way around the BOQ."""
        line = self.revision.sections.first().lines.get(description=PIPE_200)
        with self.assertRaises(DomainError) as ctx:
            procurement.raise_site_requisition(
                project=self.project, actor=self.engineer,
                lines=[{"boq_line": line, "description": line.description, "quantity": 999}],
            )
        self.assertIn("BOQ allows", str(ctx.exception))

    def test_holding_a_requisition_parks_it_rather_than_losing_it(self):
        line = self.revision.sections.first().lines.first()
        request = procurement.raise_site_requisition(
            project=self.project, actor=self.engineer,
            lines=[{"boq_line": line, "description": line.description, "quantity": 1}],
        )
        procurement.hold_request(request=request, reason="wait for the next cycle", actor=self.pm)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcurementRequest.HELD)
        self.assertEqual(request.hold_reason, "wait for the next cycle")

    def test_the_project_manager_approves_it_into_procurement(self):
        line = self.revision.sections.first().lines.first()
        request = procurement.raise_site_requisition(
            project=self.project, actor=self.engineer,
            lines=[{"boq_line": line, "description": line.description, "quantity": 1}],
        )
        procurement.approve_request(request=request, actor=self.pm)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcurementRequest.APPROVED)

    def test_someone_without_the_capability_cannot_approve_it(self):
        line = self.revision.sections.first().lines.first()
        request = procurement.raise_site_requisition(
            project=self.project, actor=self.engineer,
            lines=[{"boq_line": line, "description": line.description, "quantity": 1}],
        )
        with self.assertRaises(DomainError):
            procurement.approve_request(request=request, actor=self.buyer)


class RfqTests(ProcurementFlowTestCase):
    def test_an_rfq_needs_at_least_three_vendors(self):
        rfq = procurement.create_rfq(
            request=self.request, vendors=self.vendors[:2], actor=self.buyer
        )
        with self.assertRaises(DomainError) as ctx:
            procurement.issue_rfq(rfq=rfq, actor=self.buyer)
        self.assertIn("at least 3", str(ctx.exception))

    def test_fewer_than_three_is_allowed_with_a_recorded_reason(self):
        """A specialised item with one capable supplier is a real situation.
        Blocking it indefinitely would be worse than recording why."""
        rfq = procurement.create_rfq(
            request=self.request, vendors=self.vendors[:1], actor=self.buyer
        )
        rfq.min_vendors_waived_reason = "Only approved supplier for this specification."
        rfq.save(update_fields=["min_vendors_waived_reason"])
        procurement.issue_rfq(rfq=rfq, actor=self.buyer)
        self.assertEqual(rfq.status, Rfq.ISSUED)

    def test_the_comparison_marks_the_best_price_without_selecting_it(self):
        rfq = self._rfq_with_quotes(rates=("1200", "1150", "1310"))
        statement = procurement.comparison(rfq)
        row = next(r for r in statement if r["line"].description == PIPE_200)

        self.assertEqual(row["best_rate"], Decimal("1150"))
        best = [q for q in row["quotes"] if q["is_best_price"]]
        self.assertEqual(len(best), 1)
        self.assertIsNone(row["awarded"])

    def test_an_rfq_can_only_be_raised_from_an_approved_request(self):
        line = self.revision.sections.first().lines.first()
        pending = procurement.raise_site_requisition(
            project=self.project, actor=self.engineer,
            lines=[{"boq_line": line, "description": line.description, "quantity": 1}],
        )
        with self.assertRaises(DomainError):
            procurement.create_rfq(request=pending, vendors=self.vendors[:3], actor=self.buyer)


class AwardTests(ProcurementFlowTestCase):
    def test_the_purchase_manager_may_award_the_highest_quote(self):
        """Stated as their prerogative, and the platform does not argue."""
        rfq = self._rfq_with_quotes(rates=("1200", "1150", "1310"))
        line = self._line()
        highest = rfq.vendors.all()[2].vendor

        award = procurement.award_line(
            rfq=rfq, request_line=line, vendor=highest, actor=self.manager,
            notes="Only vendor able to deliver before the slab pour.",
        )
        self.assertEqual(award.winning_vendor, highest)
        self.assertEqual(award.awarded_rate, Decimal("1310"))
        self.assertFalse(award.was_lowest)

    def test_no_justification_is_demanded_for_awarding_above_the_lowest(self):
        rfq = self._rfq_with_quotes(rates=("1200", "1150", "1310"))
        award = procurement.award_line(
            rfq=rfq, request_line=self._line(),
            vendor=rfq.vendors.all()[2].vendor, actor=self.manager,
        )
        self.assertEqual(award.notes, "")

    def test_the_comparison_is_frozen_onto_the_award(self):
        """The audit trail is what was on screen, not a free-text reason."""
        rfq = self._rfq_with_quotes(rates=("1200", "1150", "1310"))
        award = procurement.award_line(
            rfq=rfq, request_line=self._line(),
            vendor=rfq.vendors.all()[0].vendor, actor=self.manager,
        )
        rates = {
            q["vendor"]: Decimal(q["rate"]) for q in award.comparison_snapshot["quotes"]
        }
        self.assertEqual(len(rates), 3)
        self.assertIn(Decimal("1150"), rates.values())

    def test_awarding_needs_the_capability(self):
        rfq = self._rfq_with_quotes()
        with self.assertRaises(DomainError):
            procurement.award_line(
                rfq=rfq, request_line=self._line(),
                vendor=rfq.vendors.all()[0].vendor, actor=self.buyer,
            )

    def test_a_vendor_that_did_not_quote_cannot_be_awarded(self):
        rfq = self._rfq_with_quotes()
        with self.assertRaises(DomainError):
            procurement.award_line(
                rfq=rfq, request_line=self._line(),
                vendor=self.vendors[3], actor=self.manager,
            )

    def test_awarding_is_blocked_below_three_responses_without_a_waiver(self):
        rfq = procurement.create_rfq(
            request=self.request, vendors=self.vendors[:3], actor=self.buyer
        )
        procurement.issue_rfq(rfq=rfq, actor=self.buyer)
        line = self._line()
        procurement.record_quote(
            rfq_vendor=rfq.vendors.first(),
            quotes=[{"request_line": line, "rate": "1200"}], actor=self.buyer,
        )
        with self.assertRaises(DomainError) as ctx:
            procurement.award_line(
                rfq=rfq, request_line=line, vendor=rfq.vendors.first().vendor, actor=self.manager
            )
        self.assertIn("At least 3", str(ctx.exception))


class PurchaseOrderTests(ProcurementFlowTestCase):
    def _award(self, rate="1200"):
        rfq = self._rfq_with_quotes(rates=(rate, "9999", "9998"))
        return procurement.award_line(
            rfq=rfq, request_line=self._line(),
            vendor=rfq.vendors.first().vendor, actor=self.manager,
        )

    def test_raising_an_order_consumes_the_boq_ceiling(self):
        award = self._award()
        boq_line = award.request_line.boq_line
        self.assertEqual(headroom(boq_line.pk), Decimal("18"))

        procurement.create_purchase_order(awards=[award], actor=self.manager)
        self.assertEqual(committed_qty(boq_line.pk), Decimal("18"))
        self.assertEqual(headroom(boq_line.pk), Decimal("0"))

    def test_a_second_order_beyond_the_ceiling_is_refused(self):
        award = self._award()
        procurement.create_purchase_order(awards=[award], actor=self.manager)
        with self.assertRaises(CeilingExceeded):
            procurement.create_purchase_order(awards=[award], actor=self.manager)

    def test_an_order_above_the_threshold_parks_for_the_purchase_manager(self):
        """And the parked state must survive — it is a state, not an error."""
        award = self._award()
        order = procurement.create_purchase_order(awards=[award], actor=self.buyer)
        procurement.submit_purchase_order(
            order=order, actor=self.buyer, threshold=Decimal("1000")
        )
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.AWAITING_APPROVAL)
        self.assertFalse(order.is_locked)

    def test_a_buyer_cannot_approve_their_own_parked_order(self):
        award = self._award()
        order = procurement.create_purchase_order(awards=[award], actor=self.buyer)
        procurement.submit_purchase_order(
            order=order, actor=self.buyer, threshold=Decimal("1000")
        )
        with self.assertRaises(DomainError):
            procurement.approve_purchase_order(order=order, actor=self.buyer)

    def test_the_purchase_manager_approves_it_through(self):
        award = self._award()
        order = procurement.create_purchase_order(awards=[award], actor=self.buyer)
        procurement.submit_purchase_order(
            order=order, actor=self.buyer, threshold=Decimal("1000")
        )
        procurement.approve_purchase_order(order=order, actor=self.manager)
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.CONFIRMED)
        self.assertTrue(order.is_locked)

    def test_below_the_threshold_a_buyer_confirms_directly(self):
        award = self._award()
        order = procurement.create_purchase_order(awards=[award], actor=self.buyer)
        procurement.submit_purchase_order(
            order=order, actor=self.buyer, threshold=Decimal("99999999")
        )
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.CONFIRMED)

    def test_confirming_creates_the_expected_receipt(self):
        award = self._award()
        order = procurement.create_purchase_order(awards=[award], actor=self.manager)
        procurement.submit_purchase_order(order=order, actor=self.manager)
        events.drain()

        expected = ExpectedReceipt.objects.get(purchase_order_line__purchase_order=order)
        self.assertEqual(expected.expected_qty, Decimal("18"))
        self.assertEqual(expected.location.project, self.project)

    def test_amending_an_order_down_releases_its_headroom(self):
        award = self._award()
        order = procurement.create_purchase_order(awards=[award], actor=self.manager)
        line = order.lines.first()
        self.assertEqual(headroom(line.boq_line_id), Decimal("0"))

        procurement.amend_line(
            line=line, new_qty=Decimal("12"), reason="site revised the run", actor=self.manager
        )
        self.assertEqual(headroom(line.boq_line_id), Decimal("6"))

    def test_one_order_per_vendor(self):
        rfq = self._rfq_with_quotes()
        line = self._line()
        award_a = procurement.award_line(
            rfq=rfq, request_line=line, vendor=rfq.vendors.all()[0].vendor, actor=self.manager
        )
        other_line = self.request.lines.exclude(pk=line.pk).first()
        procurement.record_quote(
            rfq_vendor=rfq.vendors.all()[1],
            quotes=[{"request_line": other_line, "rate": "500"}], actor=self.buyer,
        )
        award_b = procurement.award_line(
            rfq=rfq, request_line=other_line,
            vendor=rfq.vendors.all()[1].vendor, actor=self.manager,
        )
        with self.assertRaises(DomainError):
            procurement.create_purchase_order(awards=[award_a, award_b], actor=self.manager)


class ReceiptTests(ProcurementFlowTestCase):
    def setUp(self):
        super().setUp()
        rfq = self._rfq_with_quotes(rates=("1200", "9999", "9998"))
        award = procurement.award_line(
            rfq=rfq, request_line=self._line(),
            vendor=rfq.vendors.first().vendor, actor=self.manager,
        )
        self.order = procurement.create_purchase_order(awards=[award], actor=self.manager)
        # A real item, so stock has something to move.
        self.item = Item.objects.create(code="pipe200", name=PIPE_200, uom="Mtr")
        self.order.lines.update(item=self.item)
        procurement.submit_purchase_order(order=self.order, actor=self.manager)
        events.drain()
        self.line = self.order.lines.first()
        self.site = Location.objects.get(project=self.project, kind=Location.SITE)

    def test_a_recorded_receipt_does_not_post_cost(self):
        """The Store Keeper says it arrived. That is not the same as accepting it."""
        inventory.record_receipt(
            purchase_order_line=self.line, quantity=18, actor=self.store
        )
        self.assertEqual(costing.project_total(self.project.pk), Decimal("0"))
        self.assertEqual(on_hand(self.item.pk, self.site.pk), Decimal("0"))

    def test_verification_posts_stock_and_cost(self):
        receipt = inventory.record_receipt(
            purchase_order_line=self.line, quantity=18, actor=self.store
        )
        inventory.verify_receipt(receipt=receipt, accepted_qty=18, actor=self.engineer)

        self.assertEqual(on_hand(self.item.pk, self.site.pk), Decimal("18"))
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.MATERIAL),
            Decimal("21600"),  # 18 × 1200
        )
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, GoodsReceipt.VERIFIED)

    def test_only_a_site_engineer_may_verify(self):
        receipt = inventory.record_receipt(
            purchase_order_line=self.line, quantity=18, actor=self.store
        )
        with self.assertRaises(DomainError):
            inventory.verify_receipt(receipt=receipt, accepted_qty=18, actor=self.store)

    def test_a_receipt_cannot_be_verified_twice(self):
        receipt = inventory.record_receipt(
            purchase_order_line=self.line, quantity=18, actor=self.store
        )
        inventory.verify_receipt(receipt=receipt, accepted_qty=18, actor=self.engineer)
        with self.assertRaises(DomainError):
            inventory.verify_receipt(receipt=receipt, accepted_qty=18, actor=self.engineer)

    def test_more_cannot_be_received_than_was_ordered(self):
        with self.assertRaises(DomainError) as ctx:
            inventory.record_receipt(
                purchase_order_line=self.line, quantity=25, actor=self.store
            )
        self.assertIn("outstanding", str(ctx.exception))

    def test_a_partial_receipt_leaves_the_rest_outstanding(self):
        receipt = inventory.record_receipt(
            purchase_order_line=self.line, quantity=10, actor=self.store
        )
        inventory.verify_receipt(receipt=receipt, accepted_qty=10, actor=self.engineer)
        self.line.refresh_from_db()
        self.assertEqual(self.line.received_qty, Decimal("10"))
        self.assertEqual(self.line.outstanding_qty, Decimal("8"))

    def test_a_rejection_logs_a_discrepancy_and_holds_the_vendor_bill(self):
        receipt = inventory.record_receipt(
            purchase_order_line=self.line, quantity=18, actor=self.store
        )
        inventory.verify_receipt(
            receipt=receipt, accepted_qty=15, rejected_qty=3, actor=self.engineer,
            notes="3 Mtr dented in transit",
        )
        discrepancy = Discrepancy.objects.get()
        self.assertEqual(discrepancy.quantity, Decimal("3"))
        self.assertTrue(discrepancy.holds_vendor_bill)

    def test_only_the_accepted_quantity_becomes_cost(self):
        receipt = inventory.record_receipt(
            purchase_order_line=self.line, quantity=18, actor=self.store
        )
        inventory.verify_receipt(
            receipt=receipt, accepted_qty=15, rejected_qty=3, actor=self.engineer,
            notes="damaged",
        )
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.MATERIAL), Decimal("18000")
        )
        self.assertEqual(on_hand(self.item.pk, self.site.pk), Decimal("15"))

    def test_a_rejection_frees_headroom_so_a_replacement_can_be_ordered(self):
        receipt = inventory.record_receipt(
            purchase_order_line=self.line, quantity=18, actor=self.store
        )
        self.assertEqual(headroom(self.line.boq_line_id), Decimal("0"))
        inventory.verify_receipt(
            receipt=receipt, accepted_qty=15, rejected_qty=3, actor=self.engineer,
            notes="damaged",
        )
        self.assertEqual(headroom(self.line.boq_line_id), Decimal("3"))


class ReturnTests(ProcurementFlowTestCase):
    """Its own fixture. Inheriting ReceiptTests would re-run every receipt test
    against a line this setUp has already received in full."""

    def setUp(self):
        super().setUp()
        rfq = self._rfq_with_quotes(rates=("1200", "9999", "9998"))
        award = procurement.award_line(
            rfq=rfq, request_line=self._line(),
            vendor=rfq.vendors.first().vendor, actor=self.manager,
        )
        self.order = procurement.create_purchase_order(awards=[award], actor=self.manager)
        self.item = Item.objects.create(code="pipe200r", name=PIPE_200, uom="Mtr")
        self.order.lines.update(item=self.item)
        procurement.submit_purchase_order(order=self.order, actor=self.manager)
        events.drain()
        self.line = self.order.lines.first()
        self.site = Location.objects.get(project=self.project, kind=Location.SITE)
        self.receipt = inventory.record_receipt(
            purchase_order_line=self.line, quantity=18, actor=self.store
        )
        inventory.verify_receipt(receipt=self.receipt, accepted_qty=18, actor=self.engineer)

    def test_a_return_reverses_stock_cost_and_commitment(self):
        inventory.return_material(
            purchase_order_line=self.line, quantity=6, reason="surplus to requirement",
            actor=self.engineer, goods_receipt=self.receipt,
        )
        self.assertEqual(on_hand(self.item.pk, self.site.pk), Decimal("12"))
        self.assertEqual(
            costing.project_total(self.project.pk, CostEntry.MATERIAL), Decimal("14400")
        )
        self.assertEqual(headroom(self.line.boq_line_id), Decimal("6"))

    def test_the_freed_headroom_can_actually_be_re_ordered(self):
        """The whole point of the commitment ledger, end to end."""
        inventory.return_material(
            purchase_order_line=self.line, quantity=6, reason="damaged",
            actor=self.engineer, goods_receipt=self.receipt,
        )
        rfq = self._rfq_with_quotes(rates=("1250", "9999", "9998"))
        award = procurement.award_line(
            rfq=rfq, request_line=self._line(),
            vendor=rfq.vendors.first().vendor, actor=self.manager,
        )
        award.awarded_qty = Decimal("6")
        award.save(update_fields=["awarded_qty"])

        replacement = procurement.create_purchase_order(awards=[award], actor=self.manager)
        self.assertEqual(replacement.lines.first().quantity, Decimal("6"))
        self.assertEqual(committed_qty(self.line.boq_line_id), Decimal("18"))

    def test_more_cannot_be_returned_than_was_received(self):
        with self.assertRaises(DomainError):
            inventory.return_material(
                purchase_order_line=self.line, quantity=99, reason="x", actor=self.engineer
            )
