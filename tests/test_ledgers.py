"""Append-only enforcement, and the derived totals that depend on it.

Design principle 1: nothing increments a stored total. These tests prove the
ledgers cannot be edited through any path the application has — including the
bulk paths that bypass ``Model.save``, and including raw SQL, which is what the
database trigger is for.
"""

from datetime import date
from decimal import Decimal

from django.db import connection, transaction
from django.db.utils import InternalError, NotSupportedError
from django.test import TestCase

from apps.platform_core.models import CommitmentEntry, CostEntry, StockMove
from apps.platform_core.services import costing, stock
from apps.platform_core.services.ceiling import reserve_headroom

from .factories import make_boq_line, make_item, make_location, make_project, make_user


class AppendOnlyTests(TestCase):
    def setUp(self):
        self.actor = make_user()
        self.line = make_boq_line(quantity=Decimal("100"))
        with transaction.atomic():
            self.entry = reserve_headroom(
                boq_line_id=self.line.pk,
                qty=Decimal("10"),
                document_type="purchase_order",
                document_id=1,
                actor=self.actor,
                reason="setup",
            )

    def test_saving_an_existing_entry_is_refused(self):
        self.entry.qty_delta = Decimal("999")
        with self.assertRaises(NotImplementedError):
            self.entry.save()

    def test_deleting_an_entry_is_refused(self):
        with self.assertRaises(NotImplementedError):
            self.entry.delete()

    def test_bulk_update_is_refused(self):
        """``QuerySet.update`` bypasses ``save``, so it needs its own guard."""
        with self.assertRaises(NotImplementedError):
            CommitmentEntry.objects.filter(pk=self.entry.pk).update(
                qty_delta=Decimal("999")
            )

    def test_bulk_delete_is_refused(self):
        with self.assertRaises(NotImplementedError):
            CommitmentEntry.objects.filter(pk=self.entry.pk).delete()

    def test_raw_sql_update_is_refused_by_the_database_trigger(self):
        """The guarantee must not depend on going through the ORM.

        This is why append-only is a trigger rather than revoked grants: the
        test connection is the table owner and would bypass any grant.
        """
        with self.assertRaises((InternalError, NotSupportedError, Exception)) as ctx:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(
                        "UPDATE commitment_entry SET qty_delta = 999 WHERE id = %s",
                        [self.entry.pk],
                    )
        self.assertIn("append-only", str(ctx.exception).lower())

    def test_raw_sql_delete_is_refused_by_the_database_trigger(self):
        with self.assertRaises(Exception) as ctx:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(
                        "DELETE FROM commitment_entry WHERE id = %s", [self.entry.pk]
                    )
        self.assertIn("append-only", str(ctx.exception).lower())

    def test_the_cost_ledger_is_append_only_too(self):
        entry = costing.post_cost(
            project_id=self.line.project_id,
            category=CostEntry.MATERIAL,
            amount=Decimal("5000"),
            source_type="goods_receipt",
            source_id=1,
            actor=self.actor,
        )
        with self.assertRaises(NotImplementedError):
            entry.amount = Decimal("1")
            entry.save()

    def test_the_stock_ledger_is_append_only_too(self):
        item = make_item()
        location = make_location()
        move = stock.post_move(
            item_id=item.pk,
            quantity=Decimal("10"),
            to_location_id=location.pk,
            source_type="goods_receipt",
            source_id=1,
            actor=self.actor,
        )
        with self.assertRaises(NotImplementedError):
            move.delete()


class CostLedgerTests(TestCase):
    def setUp(self):
        self.actor = make_user()
        self.project = make_project()

    def _post(self, category, amount, lot_id=None):
        return costing.post_cost(
            project_id=self.project.pk,
            category=category,
            amount=Decimal(amount),
            source_type="test",
            source_id=1,
            actor=self.actor,
            lot_id=lot_id,
            effective_date=date(2026, 7, 1),
        )

    def test_profitability_is_revenue_less_every_cost_category(self):
        self._post(CostEntry.REVENUE, "1000000")
        self._post(CostEntry.MATERIAL, "400000")
        self._post(CostEntry.SUBCONTRACT, "250000")
        self._post(CostEntry.SITE_EXPENSE, "50000")

        result = costing.profitability(self.project.pk)
        self.assertEqual(result["revenue"], Decimal("1000000"))
        self.assertEqual(result["cost"], Decimal("700000"))
        self.assertEqual(result["margin"], Decimal("300000"))

    def test_site_expenses_reach_the_profitability_figure(self):
        """The structural fix for the gap the earlier design documented.

        Site running costs could not reach Odoo's profitability panel at all,
        so an entire category of real spend was invisible in the number the
        Project Manager is accountable for. Here it is one more cost entry.
        """
        self._post(CostEntry.REVENUE, "100000")
        before = costing.profitability(self.project.pk)["margin"]
        self._post(CostEntry.SITE_EXPENSE, "15000")
        after = costing.profitability(self.project.pk)["margin"]
        self.assertEqual(before - after, Decimal("15000"))

    def test_a_correction_is_a_reversal_not_an_edit(self):
        entry = self._post(CostEntry.MATERIAL, "5000")
        costing.reverse_cost(entry, actor=self.actor)

        self.assertEqual(costing.project_total(self.project.pk), Decimal("0"))
        # Both the original and its reversal remain on record.
        self.assertEqual(CostEntry.objects.filter(project=self.project).count(), 2)

    def test_margin_can_be_sliced_by_lot(self):
        """Per-SITC-lot margin is a query, because every entry carries its lot."""
        self._post(CostEntry.REVENUE, "500000", lot_id=1)
        self._post(CostEntry.MATERIAL, "300000", lot_id=1)
        self._post(CostEntry.REVENUE, "200000", lot_id=2)
        self._post(CostEntry.MATERIAL, "180000", lot_id=2)

        self.assertEqual(costing.lot_profitability(1)["margin"], Decimal("200000"))
        self.assertEqual(costing.lot_profitability(2)["margin"], Decimal("20000"))
        # The blended project figure hides that lot 2 is barely profitable.
        self.assertEqual(costing.profitability(self.project.pk)["margin"], Decimal("220000"))


class StockLedgerTests(TestCase):
    def setUp(self):
        self.actor = make_user()
        self.item = make_item()
        self.site_a = make_location()
        self.site_b = make_location()

    def _move(self, qty, frm=None, to=None):
        return stock.post_move(
            item_id=self.item.pk,
            quantity=Decimal(qty),
            from_location_id=frm,
            to_location_id=to,
            source_type="test",
            source_id=1,
            actor=self.actor,
        )

    def test_on_hand_is_the_sum_of_moves(self):
        self._move("100", to=self.site_a.pk)
        self._move("30", frm=self.site_a.pk)
        self.assertEqual(stock.on_hand(self.item.pk, self.site_a.pk), Decimal("70"))

    def test_a_transfer_moves_stock_between_two_projects(self):
        self._move("100", to=self.site_a.pk)
        self._move("40", frm=self.site_a.pk, to=self.site_b.pk)
        self.assertEqual(stock.on_hand(self.item.pk, self.site_a.pk), Decimal("60"))
        self.assertEqual(stock.on_hand(self.item.pk, self.site_b.pk), Decimal("40"))

    def test_availability_reports_every_location_holding_the_item(self):
        """Cross-location availability is one query, not a joined report."""
        self._move("100", to=self.site_a.pk)
        self._move("25", to=self.site_b.pk)
        rows = {r["location_id"]: r["on_hand"] for r in stock.availability(self.item.pk)}
        self.assertEqual(rows[self.site_a.pk], Decimal("100"))
        self.assertEqual(rows[self.site_b.pk], Decimal("25"))

    def test_a_move_must_have_at_least_one_endpoint(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                self._move("10")
