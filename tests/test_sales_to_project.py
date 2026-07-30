"""Phase 1: the Sales-to-Project chain.

The two things worth proving here are the hand-off that removes re-typing
(a kicked-off order becomes a project with its lots, client, committed date and
site location, with nobody entering any of it twice) and the schedule ceiling
(no phase may be planned beyond what the client was promised).
"""

from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.test import TestCase

from apps.core.models import Location, Project
from apps.platform_core.exceptions import DomainError
from apps.platform_core.models import CostEntry, OutboxEvent
from apps.platform_core.services import costing, events
from apps.projects import services as project_services
from apps.projects.models import ScheduleExtension, SchedulePhase
from apps.projects.services import ScheduleExceedsCommitment
from apps.sales import services as sales_services
from apps.sales.models import (
    ChangeOrder,
    Client,
    ClientInvoice,
    Lot,
    LotKind,
    Order,
    Quotation,
    QuotationLot,
)

from .factories import make_role, make_user

COMMITTED = date(2027, 3, 31)


def make_client(name="Acme Constructions") -> Client:
    return Client.objects.create(name=name, gstin="27AAAAA0000A1Z5")


def make_order(client=None, lots=None, number="SO-001") -> Order:
    client = client or make_client()
    order = Order.objects.create(client=client, number=number)
    for i, (name, kind, price) in enumerate(lots or [("Main works", LotKind.ITEMIZED, "1000000")], 1):
        Lot.objects.create(
            order=order, name=name, kind=kind, price=Decimal(price), sequence=i
        )
    return order


def make_sales_manager():
    user = make_user(username="sales_mgr")
    user.user_roles.create(
        role=make_role(code="sales_manager", capabilities=["order:approve_kickoff"])
    )
    return user


def make_pm():
    user = make_user(username="pm")
    user.user_roles.create(
        role=make_role(
            code="project_manager",
            capabilities=["project:extend_schedule", "order:approve_kickoff"],
        )
    )
    return user


class KickoffGateTests(TestCase):
    def setUp(self):
        self.manager = make_sales_manager()
        self.order = make_order()

    def test_a_confirmed_order_is_held_pending_review_not_kicked_off(self):
        """A confirmed order does not auto-create a project."""
        with transaction.atomic():
            sales_services.confirm_order(
                order=self.order, committed_delivery_date=COMMITTED, actor=self.manager
            )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.HELD)
        self.assertEqual(Project.objects.count(), 0)

    def test_confirming_requires_a_committed_delivery_date(self):
        with self.assertRaises(DomainError):
            with transaction.atomic():
                sales_services.confirm_order(
                    order=self.order, committed_delivery_date=None, actor=self.manager
                )

    def test_confirming_requires_at_least_one_lot(self):
        empty = Order.objects.create(client=self.order.client, number="SO-EMPTY")
        with self.assertRaises(DomainError):
            with transaction.atomic():
                sales_services.confirm_order(
                    order=empty, committed_delivery_date=COMMITTED, actor=self.manager
                )

    def test_only_an_authorized_user_can_approve_kickoff(self):
        with transaction.atomic():
            sales_services.confirm_order(
                order=self.order, committed_delivery_date=COMMITTED, actor=self.manager
            )
        nobody = make_user(username="nobody")
        with self.assertRaises(DomainError):
            with transaction.atomic():
                sales_services.approve_for_kickoff(order=self.order, actor=nobody)

    def test_a_held_order_can_be_re_reviewed_rather_than_lost(self):
        with transaction.atomic():
            sales_services.confirm_order(
                order=self.order, committed_delivery_date=COMMITTED, actor=self.manager
            )
            sales_services.hold_for_review(
                order=self.order, reason="await client PO copy", actor=self.manager
            )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.HELD)

        with transaction.atomic():
            sales_services.approve_for_kickoff(order=self.order, actor=self.manager)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.KICKED_OFF)


class ProjectInitiationTests(TestCase):
    """The automatic hand-off: approve kickoff, and the project appears."""

    def setUp(self):
        self.manager = make_sales_manager()
        self.order = make_order(
            lots=[
                ("Lot 1: Fire Fighting SITC", LotKind.LUMP_SUM_SITC, "4000000"),
                ("Lot 2: HVAC SITC", LotKind.LUMP_SUM_SITC, "2500000"),
            ]
        )
        with transaction.atomic():
            sales_services.confirm_order(
                order=self.order, committed_delivery_date=COMMITTED, actor=self.manager
            )

    def _kickoff(self):
        with transaction.atomic():
            sales_services.approve_for_kickoff(order=self.order, actor=self.manager)
        events.drain()

    def test_approving_kickoff_creates_the_project_via_the_event_bus(self):
        self.assertEqual(Project.objects.count(), 0)
        self._kickoff()

        project = Project.objects.get()
        self.assertEqual(project.order, self.order)
        self.assertEqual(project.client, self.order.client)
        self.assertEqual(project.budget, Decimal("6500000"))
        self.assertEqual(project.effective_committed_date, COMMITTED)

    def test_the_lots_carry_forward_without_re_typing(self):
        self._kickoff()
        project = Project.objects.get()
        self.assertEqual(project.lots.count(), 2)
        self.assertEqual(
            sorted(project.lots.values_list("name", flat=True)),
            ["Lot 1: Fire Fighting SITC", "Lot 2: HVAC SITC"],
        )

    def test_a_site_stock_location_is_provisioned(self):
        """Project isolation depends on the project having its own location."""
        self._kickoff()
        project = Project.objects.get()
        location = Location.objects.get(project=project)
        self.assertEqual(location.kind, Location.SITE)

    def test_initiation_is_idempotent_because_delivery_is_at_least_once(self):
        self._kickoff()
        events.drain()
        events.drain()
        self.assertEqual(Project.objects.count(), 1)

    def test_an_order_not_kicked_off_cannot_create_a_project(self):
        with self.assertRaises(DomainError):
            with transaction.atomic():
                project_services.initiate_project(order=self.order)

    def test_the_hand_off_event_is_recorded_for_audit(self):
        self._kickoff()
        names = set(OutboxEvent.objects.values_list("event_name", flat=True))
        self.assertIn("OrderApprovedForKickoff", names)
        self.assertIn("ProjectInitiated", names)


class ScheduleCeilingTests(TestCase):
    """No phase may be planned beyond the date promised to the client."""

    def setUp(self):
        self.manager = make_sales_manager()
        self.pm = make_pm()
        self.order = make_order()
        with transaction.atomic():
            sales_services.confirm_order(
                order=self.order, committed_delivery_date=COMMITTED, actor=self.manager
            )
            sales_services.approve_for_kickoff(order=self.order, actor=self.manager)
        events.drain()
        self.project = Project.objects.get()

    def _plan(self, planned_end, kind=SchedulePhase.CONSTRUCTION, name="Construction", seq=1):
        return project_services.plan_phase(
            project=self.project,
            name=name,
            kind=kind,
            planned_end=planned_end,
            sequence=seq,
            actor=self.pm,
        )

    def test_a_phase_within_the_committed_date_is_accepted(self):
        phase = self._plan(COMMITTED - timedelta(days=30))
        self.assertEqual(SchedulePhase.objects.count(), 1)
        self.assertLess(phase.planned_end, COMMITTED)

    def test_a_phase_on_the_committed_date_is_accepted(self):
        self._plan(COMMITTED)
        self.assertEqual(SchedulePhase.objects.count(), 1)

    def test_a_phase_beyond_the_committed_date_is_blocked(self):
        with self.assertRaises(ScheduleExceedsCommitment) as ctx:
            self._plan(COMMITTED + timedelta(days=1))
        self.assertEqual(ctx.exception.committed_date, COMMITTED)
        self.assertEqual(SchedulePhase.objects.count(), 0)

    def test_rescheduling_beyond_the_committed_date_is_blocked_too(self):
        """The ceiling is not only checked when a phase is first created."""
        phase = self._plan(COMMITTED - timedelta(days=30))
        with self.assertRaises(ScheduleExceedsCommitment):
            project_services.reschedule_phase(
                phase=phase, new_end=COMMITTED + timedelta(days=5), actor=self.pm
            )
        phase.refresh_from_db()
        self.assertEqual(phase.planned_end, COMMITTED - timedelta(days=30))

    def test_every_date_change_is_logged_with_its_author(self):
        phase = self._plan(COMMITTED - timedelta(days=60))
        project_services.reschedule_phase(
            phase=phase,
            new_end=COMMITTED - timedelta(days=20),
            actor=self.pm,
            reason="drawings issued late",
        )
        change = phase.date_changes.get()
        self.assertEqual(change.changed_by, self.pm)
        self.assertEqual(change.reason, "drawings issued late")
        self.assertEqual(change.previous_end, COMMITTED - timedelta(days=60))

    def test_procurement_can_be_staged_as_many_times_as_needed(self):
        """Phased delivery is just more rows — no special-casing."""
        for i, offset in enumerate([120, 90, 60], start=1):
            self._plan(
                COMMITTED - timedelta(days=offset),
                kind=SchedulePhase.PROCUREMENT,
                name=f"Procurement stage {i}",
                seq=i,
            )
        self.assertEqual(
            SchedulePhase.objects.filter(kind=SchedulePhase.PROCUREMENT).count(), 3
        )


class ScheduleExtensionTests(TestCase):
    def setUp(self):
        self.manager = make_sales_manager()
        self.pm = make_pm()
        self.order = make_order()
        with transaction.atomic():
            sales_services.confirm_order(
                order=self.order, committed_delivery_date=COMMITTED, actor=self.manager
            )
            sales_services.approve_for_kickoff(order=self.order, actor=self.manager)
        events.drain()
        self.project = Project.objects.get()
        self.later = COMMITTED + timedelta(days=45)

    def test_an_extension_raises_the_ceiling_and_unblocks_planning(self):
        with self.assertRaises(ScheduleExceedsCommitment):
            project_services.plan_phase(
                project=self.project,
                name="Construction",
                kind=SchedulePhase.CONSTRUCTION,
                planned_end=self.later,
                actor=self.pm,
            )

        project_services.extend_commitment(
            project=self.project,
            new_committed_date=self.later,
            client_agreement_reference="Client email 2026-11-02, ref DE/EXT/17",
            actor=self.pm,
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.effective_committed_date, self.later)

        phase = project_services.plan_phase(
            project=self.project,
            name="Construction",
            kind=SchedulePhase.CONSTRUCTION,
            planned_end=self.later,
            actor=self.pm,
        )
        self.assertEqual(phase.planned_end, self.later)

    def test_an_extension_requires_a_recorded_client_agreement(self):
        """A contractual date; internal sign-off alone is not evidence."""
        with self.assertRaises(DomainError):
            project_services.extend_commitment(
                project=self.project,
                new_committed_date=self.later,
                client_agreement_reference="   ",
                actor=self.pm,
            )
        self.project.refresh_from_db()
        self.assertEqual(self.project.effective_committed_date, COMMITTED)

    def test_only_the_ceo_or_project_manager_may_extend(self):
        site_engineer = make_user(username="site_engineer")
        with self.assertRaises(DomainError):
            project_services.extend_commitment(
                project=self.project,
                new_committed_date=self.later,
                client_agreement_reference="email",
                actor=site_engineer,
            )

    def test_an_extension_must_actually_be_later(self):
        with self.assertRaises(DomainError):
            project_services.extend_commitment(
                project=self.project,
                new_committed_date=COMMITTED - timedelta(days=1),
                client_agreement_reference="email",
                actor=self.pm,
            )

    def test_the_extension_is_kept_on_record(self):
        project_services.extend_commitment(
            project=self.project,
            new_committed_date=self.later,
            client_agreement_reference="signed extension letter",
            actor=self.pm,
        )
        extension = ScheduleExtension.objects.get()
        self.assertEqual(extension.previous_committed_date, COMMITTED)
        self.assertEqual(extension.new_committed_date, self.later)
        self.assertEqual(extension.authorized_by, self.pm)


class RevenueAndLotTests(TestCase):
    """Revenue posts against a lot, which is what makes per-lot margin work."""

    def setUp(self):
        self.manager = make_sales_manager()
        self.order = make_order(
            lots=[
                ("Lot 1: Fire Fighting SITC", LotKind.LUMP_SUM_SITC, "4000000"),
                ("Lot 2: HVAC SITC", LotKind.LUMP_SUM_SITC, "2500000"),
            ]
        )
        with transaction.atomic():
            sales_services.confirm_order(
                order=self.order, committed_delivery_date=COMMITTED, actor=self.manager
            )
            sales_services.approve_for_kickoff(order=self.order, actor=self.manager)
        events.drain()
        self.project = Project.objects.get()
        self.lot1, self.lot2 = list(self.project.lots.order_by("sequence"))

    def _invoice(self, lot, amount, number):
        invoice = ClientInvoice.objects.create(
            order=self.order,
            lot=lot,
            number=number,
            invoice_date=date(2026, 12, 1),
            amount=Decimal(amount),
        )
        with transaction.atomic():
            return sales_services.issue_invoice(invoice=invoice, actor=self.manager)

    def test_issuing_an_invoice_posts_revenue_against_its_lot(self):
        self._invoice(self.lot1, "1000000", "INV-1")
        entry = CostEntry.objects.get(category=CostEntry.REVENUE)
        self.assertEqual(entry.amount, Decimal("1000000"))
        self.assertEqual(entry.lot_id, self.lot1.pk)
        self.assertEqual(entry.project, self.project)

    def test_margin_can_be_read_lot_by_lot(self):
        """An order with two SITC lots shows two margins, not one blend."""
        self._invoice(self.lot1, "4000000", "INV-1")
        self._invoice(self.lot2, "2500000", "INV-2")

        actor = self.manager
        costing.post_cost(
            project_id=self.project.pk,
            lot_id=self.lot1.pk,
            category=CostEntry.MATERIAL,
            amount=Decimal("2500000"),
            source_type="test",
            source_id=1,
            actor=actor,
        )
        costing.post_cost(
            project_id=self.project.pk,
            lot_id=self.lot2.pk,
            category=CostEntry.SUBCONTRACT,
            amount=Decimal("2400000"),
            source_type="test",
            source_id=2,
            actor=actor,
        )

        self.assertEqual(
            costing.lot_profitability(self.lot1.pk)["margin"], Decimal("1500000")
        )
        # Lot 2 is barely profitable — invisible in the blended project figure.
        self.assertEqual(
            costing.lot_profitability(self.lot2.pk)["margin"], Decimal("100000")
        )
        self.assertEqual(
            costing.profitability(self.project.pk)["margin"], Decimal("1600000")
        )

    def test_an_invoice_cannot_be_issued_twice(self):
        invoice = self._invoice(self.lot1, "500000", "INV-1")
        with self.assertRaises(DomainError):
            with transaction.atomic():
                sales_services.issue_invoice(invoice=invoice, actor=self.manager)


class ChangeOrderTests(TestCase):
    def setUp(self):
        self.manager = make_sales_manager()
        self.order = make_order()
        with transaction.atomic():
            sales_services.confirm_order(
                order=self.order, committed_delivery_date=COMMITTED, actor=self.manager
            )
        self.lot = self.order.lots.get()

    def test_a_change_order_is_the_only_way_the_price_moves(self):
        change = ChangeOrder.objects.create(
            order=self.order,
            lot=self.lot,
            number="CO-1",
            price_delta=Decimal("250000"),
            reason="additional scope agreed",
        )
        with transaction.atomic():
            sales_services.apply_change_order(change_order=change, actor=self.manager)

        self.lot.refresh_from_db()
        self.assertEqual(self.lot.price, Decimal("1250000"))

    def test_a_change_order_can_move_the_committed_delivery_date(self):
        new_date = COMMITTED + timedelta(days=30)
        change = ChangeOrder.objects.create(
            order=self.order,
            number="CO-2",
            new_committed_date=new_date,
            reason="client extended scope and timeline",
        )
        with transaction.atomic():
            sales_services.apply_change_order(change_order=change, actor=self.manager)

        self.order.refresh_from_db()
        self.assertEqual(self.order.committed_delivery_date, new_date)

    def test_an_applied_change_order_locks(self):
        change = ChangeOrder.objects.create(
            order=self.order,
            lot=self.lot,
            number="CO-3",
            price_delta=Decimal("100"),
            reason="rounding correction",
        )
        with transaction.atomic():
            sales_services.apply_change_order(change_order=change, actor=self.manager)
        change.refresh_from_db()
        self.assertTrue(change.is_locked)
