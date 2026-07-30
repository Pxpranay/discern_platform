"""Phase 2: BOQ revisions, sign-off, release and reconciliation.

The reconciliation tests run against **Discern's actual BOQ documents** for the
LINAC Building fire protection work — `BOQ_Linac_Bldg_Rev0.xlsx` (06.07.2026)
and `BOQ_Linac_Bldg_Rev1.xlsx` (28.07.2026), in `tests/fixtures/`. The build
plan asked for real revisions rather than synthetic ones precisely because
hand-written documents contain things nobody invents when writing fixtures.

This pair turned out to contain three:

* An inconsistent description — "MS ERW Pipe 100 mm Nb" against "…80 mm NB" in
  the same nine-line table. Raw string matching would treat that as a deleted
  line plus an unrelated new one.
* A removal expressed as **quantity 0** with the row and SL number kept, rather
  than the row being deleted.
* A new line appended at the end, so SL numbers stay stable across revisions.

Rev 0 → Rev 1, in full:

======  ==========================================  =======  =======  ==========
SL      Description                                  Rev 0    Rev 1    Change
======  ==========================================  =======  =======  ==========
1       MS ERW Pipe 200 mm NB X 6 mm thk                 18       40   +22
2       MS ERW Pipe 150 mm NB                            30       30   —
3       MS ERW Pipe 100 mm Nb                             6        6   —
4       MS ERW Pipe 80 mm NB                              6        0   removed
5       MS ERW Pipe 65 mm NB                              6        6   —
6       MS ERW Pipe 25 mm NB                             18       24   +6
7       CI Gate valve Size 150 mm NB                      4        4   —
8       CI Gate valve Size 80 mm NB                       2        2   —
9       CI Gate valve Size 65 mm NB                       2        2   —
10      CI Gate valve Size 50 mm NB                       —        1   new
======  ==========================================  =======  =======  ==========
"""

from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from apps.core.models import BoqLine, Location
from apps.engineering import importers, reconciliation
from apps.engineering import services as boq_services
from apps.engineering.models import BoqRevision, Discipline, ReconciliationOutcome
from apps.platform_core.exceptions import DomainError, RecordLocked
from apps.platform_core.services import events
from apps.platform_core.services.ceiling import reserve_headroom
from apps.platform_core.services.stock import post_move

from .factories import make_item, make_project, make_role, make_user

FIXTURES = Path(__file__).parent / "fixtures"
REV0 = FIXTURES / "BOQ_Linac_Bldg_Rev0.xlsx"
REV1 = FIXTURES / "BOQ_Linac_Bldg_Rev1.xlsx"

PIPE_200 = "MS ERW Pipe 200 mm NB X 6 mm thk"
PIPE_100 = "MS ERW Pipe 100 mm Nb"
PIPE_80 = "MS ERW Pipe 80 mm NB"
PIPE_25 = "MS ERW Pipe 25 mm NB"
VALVE_50 = "CI Gate valve Size 50 mm NB"


def _role(code, capabilities=None):
    from apps.accounts.models import Role

    role, _ = Role.objects.get_or_create(
        code=code,
        defaults={"name": code.replace("_", " ").title(), "capabilities": capabilities or []},
    )
    return role


def make_design_manager():
    user = make_user()
    user.user_roles.create(role=_role("design_manager"))
    return user


def make_project_manager():
    user = make_user()
    user.user_roles.create(
        role=_role("pm_release", capabilities=["boq_revision:release"])
    )
    return user


class WorkbookParsingTests(TestCase):
    """The importer must cope with the real file's banner and header rows."""

    def test_rev0_parses_to_nine_lines(self):
        parsed = importers.parse_workbook(REV0)
        self.assertEqual(len(parsed["rows"]), 9)
        self.assertIn("Rev-0", parsed["revision_label"])

    def test_rev1_parses_to_ten_lines_including_the_zeroed_one(self):
        parsed = importers.parse_workbook(REV1)
        self.assertEqual(len(parsed["rows"]), 10)
        self.assertIn("Rev-1", parsed["revision_label"])

        by_desc = {r["description"]: r for r in parsed["rows"]}
        self.assertEqual(by_desc[PIPE_80]["quantity"], Decimal("0"))
        self.assertEqual(by_desc[PIPE_200]["quantity"], Decimal("40"))
        self.assertEqual(by_desc[VALVE_50]["quantity"], Decimal("1"))

    def test_the_project_banner_is_not_read_as_a_line(self):
        parsed = importers.parse_workbook(REV0)
        for row in parsed["rows"]:
            self.assertNotIn("PROJECT:", row["description"])
            self.assertNotIn("Rev-", row["description"])

    def test_units_survive_the_import(self):
        parsed = importers.parse_workbook(REV0)
        units = {r["uom"] for r in parsed["rows"]}
        self.assertEqual(units, {"Mtr", "Nos"})


class DescriptionNormalisationTests(TestCase):
    """The 'Nb' / 'NB' inconsistency is in Discern's own document."""

    def test_case_differences_do_not_break_line_identity(self):
        self.assertEqual(
            reconciliation.normalize("MS ERW Pipe 100 mm Nb"),
            reconciliation.normalize("MS ERW Pipe 100 mm NB"),
        )

    def test_whitespace_differences_do_not_break_line_identity(self):
        self.assertEqual(
            reconciliation.normalize("CI Gate  valve   Size 50 mm NB"),
            reconciliation.normalize("CI Gate valve Size 50 mm NB"),
        )

    def test_genuinely_different_items_stay_distinct(self):
        self.assertNotEqual(
            reconciliation.normalize(PIPE_80),
            reconciliation.normalize("MS ERW Pipe 65 mm NB"),
        )


class RealRevisionReconciliationTests(TestCase):
    """Rev 0 → Rev 1 with nothing yet ordered."""

    def setUp(self):
        self.project = make_project(code="linac")
        self.pm = make_project_manager()

        self.rev0 = importers.import_revision(
            path=REV0,
            project=self.project,
            revision_number=0,
            signed_off_by=make_design_manager(),
        )
        with transaction.atomic():
            boq_services.release_revision(revision=self.rev0, actor=self.pm)
        events.drain()

        self.rev1 = importers.import_revision(
            path=REV1,
            project=self.project,
            revision_number=1,
            signed_off_by=make_design_manager(),
        )

    def _outcomes_by_description(self, result):
        return {o.description: o for o in result.outcomes}

    def test_the_diff_matches_the_two_documents(self):
        result = reconciliation.reconcile(self.rev1)
        summary = result.summary()

        self.assertEqual(summary.get(ReconciliationOutcome.NEW), 1)
        self.assertEqual(summary.get(ReconciliationOutcome.INCREASED), 2)
        self.assertEqual(summary.get(ReconciliationOutcome.DECREASED_UNCOMMITTED), 1)
        self.assertEqual(summary.get(ReconciliationOutcome.UNCHANGED), 6)
        self.assertEqual(len(result.outcomes), 10)

    def test_only_the_delta_is_requested_never_the_whole_line(self):
        """The 200 mm pipe went 18 → 40. Procurement must see 22, not 40."""
        outcome = self._outcomes_by_description(reconciliation.reconcile(self.rev1))[PIPE_200]
        self.assertEqual(outcome.kind, ReconciliationOutcome.INCREASED)
        self.assertEqual(outcome.previous_qty, Decimal("18"))
        self.assertEqual(outcome.new_qty, Decimal("40"))
        self.assertEqual(outcome.delta, Decimal("22"))
        self.assertEqual(outcome.action, ReconciliationOutcome.REQUEST_DELTA)

    def test_the_second_increase_is_picked_up_too(self):
        outcome = self._outcomes_by_description(reconciliation.reconcile(self.rev1))[PIPE_25]
        self.assertEqual(outcome.delta, Decimal("6"))

    def test_a_new_line_requests_its_full_quantity(self):
        outcome = self._outcomes_by_description(reconciliation.reconcile(self.rev1))[VALVE_50]
        self.assertEqual(outcome.kind, ReconciliationOutcome.NEW)
        self.assertEqual(outcome.previous_qty, Decimal("0"))
        self.assertEqual(outcome.new_qty, Decimal("1"))
        self.assertEqual(outcome.action, ReconciliationOutcome.REQUEST_DELTA)

    def test_a_line_zeroed_to_removal_is_recognised_as_a_removal(self):
        """Discern keeps the row and sets QTY to 0 rather than deleting it."""
        outcome = self._outcomes_by_description(reconciliation.reconcile(self.rev1))[PIPE_80]
        self.assertEqual(outcome.previous_qty, Decimal("6"))
        self.assertEqual(outcome.new_qty, Decimal("0"))
        self.assertTrue(outcome.is_removal)

    def test_removing_an_unordered_line_is_the_quiet_outcome(self):
        """Nothing committed, so no vendor contact and nothing queued."""
        outcome = self._outcomes_by_description(reconciliation.reconcile(self.rev1))[PIPE_80]
        self.assertEqual(outcome.kind, ReconciliationOutcome.DECREASED_UNCOMMITTED)
        self.assertEqual(outcome.action, ReconciliationOutcome.REDUCE_DRAFT)
        self.assertEqual(outcome.excess_received, Decimal("0"))

    def test_untouched_lines_produce_no_action(self):
        result = reconciliation.reconcile(self.rev1)
        unchanged = result.of_kind(ReconciliationOutcome.UNCHANGED)
        self.assertEqual(len(unchanged), 6)
        for outcome in unchanged:
            self.assertEqual(outcome.action, ReconciliationOutcome.NONE)

    def test_procurement_sees_only_the_four_changed_lines(self):
        """Six of ten lines are untouched and must never reach Procurement."""
        result = reconciliation.reconcile(self.rev1)
        self.assertEqual(len(result.requiring_action()), 4)

    def test_the_inconsistently_cased_line_is_matched_not_duplicated(self):
        """'100 mm Nb' must not read as removed-and-re-added."""
        outcome = self._outcomes_by_description(reconciliation.reconcile(self.rev1))[PIPE_100]
        self.assertEqual(outcome.kind, ReconciliationOutcome.UNCHANGED)
        self.assertIsNotNone(outcome.previous_line)


class ReconciliationAgainstLedgersTests(TestCase):
    """The engine reads the ledgers, not the previous revision's text.

    This is the difference that matters on a live project: what the engineer
    changed is not the same number as what still needs doing once orders are in
    flight.
    """

    def setUp(self):
        self.project = make_project(code="linac2")
        self.pm = make_project_manager()
        self.actor = make_user(username="buyer")
        self.location = Location.objects.create(
            code="linac2-site", name="Site", kind=Location.SITE, project=self.project
        )

        self.rev0 = importers.import_revision(
            path=REV0,
            project=self.project,
            revision_number=0,
            signed_off_by=make_design_manager(),
        )
        with transaction.atomic():
            boq_services.release_revision(revision=self.rev0, actor=self.pm)
        events.drain()
        self.pipe80 = BoqLine.objects.get(
            section__revision=self.rev0, description=PIPE_80
        )

    def _import_rev1(self):
        return importers.import_revision(
            path=REV1,
            project=self.project,
            revision_number=1,
            signed_off_by=make_design_manager(),
        )

    def _outcome_for(self, revision, description):
        return {o.description: o for o in reconciliation.reconcile(revision).outcomes}[
            description
        ]

    def test_a_removal_of_already_ordered_material_amends_the_order(self):
        """All 6 m of 80 mm pipe on order, none delivered, then cut to zero."""
        with transaction.atomic():
            reserve_headroom(
                boq_line_id=self.pipe80.pk,
                qty=Decimal("6"),
                document_type="purchase_order",
                document_id=1,
                actor=self.actor,
                reason="ordered",
            )
        outcome = self._outcome_for(self._import_rev1(), PIPE_80)

        self.assertEqual(outcome.kind, ReconciliationOutcome.DECREASED_ORDERED)
        self.assertEqual(outcome.action, ReconciliationOutcome.AMEND_ORDER)
        self.assertEqual(outcome.order_reduction, Decimal("6"))
        self.assertEqual(outcome.excess_received, Decimal("0"))

    def test_a_removal_of_already_received_material_goes_to_the_return_queue(self):
        """Same cut, but the pipe is already at site. Different problem."""
        item = make_item()
        with transaction.atomic():
            reserve_headroom(
                boq_line_id=self.pipe80.pk,
                qty=Decimal("6"),
                document_type="purchase_order",
                document_id=1,
                actor=self.actor,
                reason="ordered",
            )
        post_move(
            item_id=item.pk,
            quantity=Decimal("6"),
            to_location_id=self.location.pk,
            source_type="goods_receipt",
            source_id=1,
            actor=self.actor,
            effective_at=timezone.now(),
            boq_line_id=self.pipe80.pk,
        )

        outcome = self._outcome_for(self._import_rev1(), PIPE_80)
        self.assertEqual(outcome.kind, ReconciliationOutcome.DECREASED_RECEIVED)
        self.assertEqual(outcome.action, ReconciliationOutcome.RETURN_QUEUE)
        self.assertEqual(outcome.excess_received, Decimal("6"))

    def test_a_partial_receipt_splits_between_return_and_amendment(self):
        """6 ordered, 4 delivered, requirement cut to zero.

        Four metres must come back; the outstanding two must be cancelled with
        the vendor before they ship. One line, two different actions.
        """
        item = make_item()
        with transaction.atomic():
            reserve_headroom(
                boq_line_id=self.pipe80.pk,
                qty=Decimal("6"),
                document_type="purchase_order",
                document_id=1,
                actor=self.actor,
                reason="ordered",
            )
        post_move(
            item_id=item.pk,
            quantity=Decimal("4"),
            to_location_id=self.location.pk,
            source_type="goods_receipt",
            source_id=1,
            actor=self.actor,
            effective_at=timezone.now(),
            boq_line_id=self.pipe80.pk,
        )

        outcome = self._outcome_for(self._import_rev1(), PIPE_80)
        self.assertEqual(outcome.kind, ReconciliationOutcome.DECREASED_RECEIVED)
        self.assertEqual(outcome.excess_received, Decimal("4"))
        self.assertEqual(outcome.order_reduction, Decimal("2"))

    def test_an_increase_still_only_requests_the_delta_when_partly_ordered(self):
        """18 → 40 with 18 already ordered. The ask is 22, not 40."""
        pipe200 = BoqLine.objects.get(section__revision=self.rev0, description=PIPE_200)
        with transaction.atomic():
            reserve_headroom(
                boq_line_id=pipe200.pk,
                qty=Decimal("18"),
                document_type="purchase_order",
                document_id=2,
                actor=self.actor,
                reason="original order",
            )
        outcome = self._outcome_for(self._import_rev1(), PIPE_200)
        self.assertEqual(outcome.committed_qty, Decimal("18"))
        self.assertEqual(outcome.delta, Decimal("22"))
        self.assertEqual(outcome.action, ReconciliationOutcome.REQUEST_DELTA)


class ReleaseGateTests(TestCase):
    def setUp(self):
        self.project = make_project(code="gate")
        self.pm = make_project_manager()
        self.design_mgr = make_design_manager()
        self.revision = boq_services.open_revision(project=self.project)
        self.goods = self.revision.sections.get(discipline=Discipline.GOODS)
        self.service = self.revision.sections.get(discipline=Discipline.SERVICE)
        BoqLine.objects.create(
            project=self.project,
            section=self.goods,
            description="Cement OPC 43",
            quantity=Decimal("500"),
            uom="bag",
        )

    def test_release_is_blocked_while_a_section_is_unsigned(self):
        with self.assertRaises(DomainError) as ctx:
            with transaction.atomic():
                boq_services.release_revision(revision=self.revision, actor=self.pm)
        self.assertIn("not signed off", str(ctx.exception))

    def test_an_empty_section_marked_not_applicable_does_not_deadlock_release(self):
        """The failure the earlier two-document design had.

        A materials-only project has no service scope, so nobody can sign the
        Service BOQ off — and under the old design nothing could proceed.
        """
        boq_services.sign_off_section(section=self.goods, actor=self.design_mgr)
        boq_services.mark_section_not_applicable(
            section=self.service, actor=self.design_mgr
        )

        with transaction.atomic():
            boq_services.release_revision(revision=self.revision, actor=self.pm)
        self.revision.refresh_from_db()
        self.assertTrue(self.revision.is_released)

    def test_an_empty_section_cannot_simply_be_signed_off(self):
        with self.assertRaises(DomainError):
            boq_services.sign_off_section(section=self.service, actor=self.design_mgr)

    def test_a_section_with_lines_cannot_be_marked_not_applicable(self):
        with self.assertRaises(DomainError):
            boq_services.mark_section_not_applicable(
                section=self.goods, actor=self.design_mgr
            )

    def test_only_an_authorised_user_may_release(self):
        boq_services.sign_off_section(section=self.goods, actor=self.design_mgr)
        boq_services.mark_section_not_applicable(
            section=self.service, actor=self.design_mgr
        )
        with self.assertRaises(DomainError):
            with transaction.atomic():
                boq_services.release_revision(
                    revision=self.revision, actor=self.design_mgr
                )

    def test_a_released_revision_is_locked(self):
        boq_services.sign_off_section(section=self.goods, actor=self.design_mgr)
        boq_services.mark_section_not_applicable(
            section=self.service, actor=self.design_mgr
        )
        with transaction.atomic():
            boq_services.release_revision(revision=self.revision, actor=self.pm)

        self.revision.refresh_from_db()
        self.revision.status = BoqRevision.DRAFT
        with self.assertRaises(RecordLocked):
            self.revision.save()

    def test_sending_back_clears_the_signatures(self):
        boq_services.sign_off_section(section=self.goods, actor=self.design_mgr)
        boq_services.send_back(
            revision=self.revision, reason="quantities need checking", actor=self.pm
        )
        self.goods.refresh_from_db()
        self.assertIsNone(self.goods.signed_off_at)
        self.assertEqual(self.revision.status, BoqRevision.DRAFT)

    def test_only_one_revision_may_be_open_at_a_time(self):
        with self.assertRaises(DomainError):
            boq_services.open_revision(project=self.project)


class RevisionCopyForwardTests(TestCase):
    """The in-app path: copying a released revision forward sets line identity."""

    def setUp(self):
        self.project = make_project(code="copyfwd")
        self.pm = make_project_manager()
        self.design_mgr = make_design_manager()

        self.rev0 = importers.import_revision(
            path=REV0,
            project=self.project,
            revision_number=0,
            signed_off_by=make_design_manager(),
        )
        with transaction.atomic():
            boq_services.release_revision(revision=self.rev0, actor=self.pm)
        events.drain()

    def test_copy_forward_carries_every_line_with_its_previous_link(self):
        rev1 = boq_services.open_revision(project=self.project)
        lines = BoqLine.objects.filter(section__revision=rev1)
        self.assertEqual(lines.count(), 9)
        self.assertTrue(all(line.previous_line_id is not None for line in lines))

    def test_a_copied_forward_revision_with_no_edits_reconciles_to_nothing(self):
        rev1 = boq_services.open_revision(project=self.project)
        result = reconciliation.reconcile(rev1)
        self.assertEqual(result.requiring_action(), [])
        self.assertEqual(len(result.of_kind(ReconciliationOutcome.UNCHANGED)), 9)

    def test_identity_survives_a_retyped_description(self):
        """The reason previous_line exists.

        Someone corrects "100 mm Nb" to "100 mm NB" while revising. Without an
        explicit link that reads as a deletion plus an unrelated addition —
        a spurious return and a spurious purchase for a line nobody touched.
        """
        rev1 = boq_services.open_revision(project=self.project)
        line = BoqLine.objects.get(section__revision=rev1, description=PIPE_100)
        line.description = "MS ERW PIPE 100MM NB (corrected)"
        line.save()

        outcomes = {
            o.boq_line.pk: o
            for o in reconciliation.reconcile(rev1).outcomes
            if o.boq_line is not None
        }
        self.assertEqual(outcomes[line.pk].kind, ReconciliationOutcome.UNCHANGED)
        self.assertEqual(len(reconciliation.reconcile(rev1).requiring_action()), 0)


class ReconciliationPersistenceTests(TestCase):
    """Release fires reconciliation automatically, and it is recorded."""

    def setUp(self):
        self.project = make_project(code="persist")
        self.pm = make_project_manager()
        self.rev0 = importers.import_revision(
            path=REV0,
            project=self.project,
            revision_number=0,
            signed_off_by=make_design_manager(),
        )
        with transaction.atomic():
            boq_services.release_revision(revision=self.rev0, actor=self.pm)
        events.drain()

    def test_releasing_a_revision_reconciles_it_without_being_asked(self):
        rev1 = importers.import_revision(
            path=REV1,
            project=self.project,
            revision_number=1,
            signed_off_by=make_design_manager(),
        )
        with transaction.atomic():
            boq_services.release_revision(revision=rev1, actor=self.pm)
        events.drain()

        outcomes = ReconciliationOutcome.objects.filter(revision=rev1)
        self.assertEqual(outcomes.count(), 10)
        self.assertEqual(
            outcomes.filter(action=ReconciliationOutcome.REQUEST_DELTA).count(), 3
        )

    def test_reconciliation_is_not_duplicated_by_a_replayed_event(self):
        rev1 = importers.import_revision(
            path=REV1,
            project=self.project,
            revision_number=1,
            signed_off_by=make_design_manager(),
        )
        with transaction.atomic():
            boq_services.release_revision(revision=rev1, actor=self.pm)
        events.drain()
        events.drain()
        self.assertEqual(ReconciliationOutcome.objects.filter(revision=rev1).count(), 10)

    def test_the_recorded_deltas_are_what_procurement_will_act_on(self):
        rev1 = importers.import_revision(
            path=REV1,
            project=self.project,
            revision_number=1,
            signed_off_by=make_design_manager(),
        )
        with transaction.atomic():
            boq_services.release_revision(revision=rev1, actor=self.pm)
        events.drain()

        deltas = {
            o.description: o.delta
            for o in ReconciliationOutcome.objects.filter(
                revision=rev1, action=ReconciliationOutcome.REQUEST_DELTA
            )
        }
        self.assertEqual(
            deltas,
            {PIPE_200: Decimal("22"), PIPE_25: Decimal("6"), VALVE_50: Decimal("1")},
        )
