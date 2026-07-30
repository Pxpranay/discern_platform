"""Property-based tests for the BOQ quantity ceiling.

Build plan §2 names this the first test to write, because it protects the
design's central promise: the platform must refuse to authorize more against a
BOQ line than the latest released revision permits, no matter what sequence of
orders, amendments, cancellations and returns leads up to it.

Three invariants are asserted after *every* operation in a randomized sequence:

1. Committed quantity never exceeds the ceiling.
2. Committed quantity never goes negative.
3. The derived sum always equals what actually happened.

Plus the closing property: release everything, and headroom returns to exactly
the full ceiling — the regression guard for the defect in the earlier design,
where a return left headroom permanently consumed.
"""

from decimal import Decimal

from django.db import transaction
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra.django import TestCase

from apps.core.models import BoqLine, Item, ItemCategory, Project
from apps.platform_core.exceptions import CeilingExceeded, OverRelease
from apps.platform_core.services.ceiling import (
    ceiling_for,
    committed_qty,
    document_holding,
    headroom,
    release_headroom,
    reserve_headroom,
)

from .factories import make_boq_line, make_user

ZERO = Decimal("0")

# Operations: reserve a quantity, or release from a previously used document.
operations = st.lists(
    st.tuples(
        st.sampled_from(["reserve", "release"]),
        st.integers(min_value=1, max_value=40),
    ),
    min_size=0,
    max_size=30,
)


class CeilingPropertyTests(TestCase):
    """Randomized sequences must never break the ceiling's arithmetic."""

    @settings(
        max_examples=150,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(ops=operations)
    def test_ceiling_invariants_hold_under_any_sequence(self, ops):
        actor = make_user()
        line = make_boq_line(quantity=Decimal("100"))
        ceiling = ceiling_for(line)

        expected = ZERO
        # Documents currently holding commitment: {doc_id: qty}
        holdings: dict[int, Decimal] = {}
        next_doc_id = 1

        for action, amount in ops:
            amount = Decimal(amount)

            if action == "reserve":
                available = ceiling - expected
                doc_id = next_doc_id
                next_doc_id += 1
                try:
                    with transaction.atomic():
                        reserve_headroom(
                            boq_line_id=line.pk,
                            qty=amount,
                            document_type="test_order",
                            document_id=doc_id,
                            actor=actor,
                            reason="property test",
                        )
                except CeilingExceeded:
                    # Must only ever block a genuine over-request. A false
                    # block is as much a defect as a missed one.
                    self.assertGreater(
                        amount,
                        available,
                        "blocked a request that fitted within the headroom",
                    )
                    continue
                self.assertLessEqual(
                    amount, available, "allowed a request beyond the headroom"
                )
                expected += amount
                holdings[doc_id] = holdings.get(doc_id, ZERO) + amount

            else:  # release
                if not holdings:
                    continue
                doc_id = next(iter(holdings))
                held = holdings[doc_id]
                to_release = min(amount, held)
                with transaction.atomic():
                    release_headroom(
                        boq_line_id=line.pk,
                        qty=to_release,
                        document_type="test_order",
                        document_id=doc_id,
                        actor=actor,
                        reason="property test release",
                    )
                expected -= to_release
                if held == to_release:
                    del holdings[doc_id]
                else:
                    holdings[doc_id] = held - to_release

            # --- invariants, after every single operation ---
            actual = committed_qty(line.pk)
            self.assertEqual(actual, expected, "derived sum diverged from reality")
            self.assertLessEqual(actual, ceiling, "committed exceeded the ceiling")
            self.assertGreaterEqual(actual, ZERO, "committed went negative")

        # --- closing property: full release restores full headroom ---
        for doc_id, held in list(holdings.items()):
            with transaction.atomic():
                release_headroom(
                    boq_line_id=line.pk,
                    qty=held,
                    document_type="test_order",
                    document_id=doc_id,
                    actor=actor,
                    reason="drain",
                )
        self.assertEqual(committed_qty(line.pk), ZERO)
        self.assertEqual(headroom(line.pk), ceiling)


class CeilingRegressionTests(TestCase):
    """Named tests for the specific defects this design set out to fix."""

    def setUp(self):
        self.actor = make_user()
        self.line = make_boq_line(quantity=Decimal("100"))

    def _reserve(self, qty, doc_id=1, **kwargs):
        with transaction.atomic():
            return reserve_headroom(
                boq_line_id=self.line.pk,
                qty=Decimal(qty),
                document_type="purchase_order",
                document_id=doc_id,
                actor=self.actor,
                reason="test",
                **kwargs,
            )

    def _release(self, qty, doc_id=1):
        with transaction.atomic():
            return release_headroom(
                boq_line_id=self.line.pk,
                qty=Decimal(qty),
                document_type="purchase_order",
                document_id=doc_id,
                actor=self.actor,
                reason="test release",
            )

    def test_return_releases_headroom_so_the_material_can_be_reordered(self):
        """The defect that motivated the commitment ledger.

        The earlier design summed non-cancelled purchase order lines. A return
        left the order line's quantity intact, so headroom was consumed forever
        and re-ordering the returned material was impossible without a BOQ
        revision that raised a quantity nobody actually wanted raised.
        """
        self._reserve(100)
        self.assertEqual(headroom(self.line.pk), ZERO)

        # 30 units arrive damaged and are returned to the vendor.
        self._release(30)
        self.assertEqual(headroom(self.line.pk), Decimal("30"))

        # The replacement must be orderable. Under the old scheme, this raised.
        self._reserve(30, doc_id=2)
        self.assertEqual(committed_qty(self.line.pk), Decimal("100"))

    def test_ceiling_blocks_the_unit_beyond_the_line(self):
        self._reserve(100)
        with self.assertRaises(CeilingExceeded) as ctx:
            self._reserve(1, doc_id=2)
        self.assertEqual(ctx.exception.available, ZERO)
        self.assertEqual(ctx.exception.requested, Decimal("1"))

    def test_blocked_document_reports_the_available_headroom(self):
        """A block must say how much was available, not merely that it failed."""
        self._reserve(70)
        with self.assertRaises(CeilingExceeded) as ctx:
            self._reserve(40, doc_id=2)
        self.assertEqual(ctx.exception.available, Decimal("30"))
        self.assertIn("30", str(ctx.exception))

    def test_amendment_down_frees_headroom_for_another_document(self):
        self._reserve(80, doc_id=1)
        self._release(30, doc_id=1)  # amended 80 -> 50
        self._reserve(50, doc_id=2)
        self.assertEqual(committed_qty(self.line.pk), Decimal("100"))

    def test_a_document_cannot_release_more_than_it_holds(self):
        """Without this guard a duplicated return manufactures headroom."""
        self._reserve(40, doc_id=1)
        with self.assertRaises(OverRelease):
            self._release(41, doc_id=1)
        self.assertEqual(committed_qty(self.line.pk), Decimal("40"))

    def test_one_document_cannot_release_anothers_commitment(self):
        self._reserve(40, doc_id=1)
        with self.assertRaises(OverRelease):
            self._release(40, doc_id=99)
        self.assertEqual(committed_qty(self.line.pk), Decimal("40"))

    def test_all_four_document_types_share_one_ceiling(self):
        """Purchase, service, fabrication and site requisition draw on the
        same headroom — the ceiling is per BOQ line, not per document type."""
        for i, doc_type in enumerate(
            ["purchase_order", "service_order", "fabrication_order", "site_requisition"]
        ):
            with transaction.atomic():
                reserve_headroom(
                    boq_line_id=self.line.pk,
                    qty=Decimal("25"),
                    document_type=doc_type,
                    document_id=i + 1,
                    actor=self.actor,
                    reason="test",
                )
        self.assertEqual(committed_qty(self.line.pk), Decimal("100"))
        with self.assertRaises(CeilingExceeded):
            with transaction.atomic():
                reserve_headroom(
                    boq_line_id=self.line.pk,
                    qty=Decimal("1"),
                    document_type="purchase_order",
                    document_id=999,
                    actor=self.actor,
                    reason="one too many",
                )

    def test_document_holding_tracks_each_document_separately(self):
        self._reserve(30, doc_id=1)
        self._reserve(20, doc_id=2)
        self.assertEqual(
            document_holding(self.line.pk, "purchase_order", 1), Decimal("30")
        )
        self.assertEqual(
            document_holding(self.line.pk, "purchase_order", 2), Decimal("20")
        )


class CeilingToleranceTests(TestCase):
    """Decision #1: wastage tolerance, per item category, default zero."""

    def setUp(self):
        self.actor = make_user()

    def test_default_category_has_no_tolerance(self):
        line = make_boq_line(quantity=Decimal("100"))
        self.assertEqual(ceiling_for(line), Decimal("100"))

    def test_consumable_category_permits_its_configured_wastage(self):
        cement = ItemCategory.objects.create(
            code="cement", name="Cement", wastage_tolerance_pct=Decimal("5")
        )
        line = make_boq_line(quantity=Decimal("100"), category=cement)
        self.assertEqual(ceiling_for(line), Decimal("105"))

        with transaction.atomic():
            reserve_headroom(
                boq_line_id=line.pk,
                qty=Decimal("105"),
                document_type="purchase_order",
                document_id=1,
                actor=self.actor,
                reason="within tolerance",
            )
        with self.assertRaises(CeilingExceeded):
            with transaction.atomic():
                reserve_headroom(
                    boq_line_id=line.pk,
                    qty=Decimal("1"),
                    document_type="purchase_order",
                    document_id=2,
                    actor=self.actor,
                    reason="beyond tolerance",
                )


class CeilingOverrideTests(TestCase):
    """Decision #2: the Project Manager's logged emergency override."""

    def setUp(self):
        self.actor = make_user()
        self.pm = make_user(username="pm", is_administrator=False)
        self.line = make_boq_line(quantity=Decimal("100"))

    def test_override_permits_exceeding_the_ceiling_and_records_why(self):
        with transaction.atomic():
            reserve_headroom(
                boq_line_id=self.line.pk,
                qty=Decimal("100"),
                document_type="purchase_order",
                document_id=1,
                actor=self.actor,
                reason="normal",
            )
        with transaction.atomic():
            entry = reserve_headroom(
                boq_line_id=self.line.pk,
                qty=Decimal("20"),
                document_type="purchase_order",
                document_id=2,
                actor=self.actor,
                reason="urgent site requirement",
                override_actor=self.pm,
                override_reason="slab pour tomorrow, cannot wait for revision",
            )
        self.assertEqual(committed_qty(self.line.pk), Decimal("120"))
        self.assertIn("CEILING OVERRIDE", entry.reason)
        self.assertIn("slab pour tomorrow", entry.reason)
        self.assertEqual(entry.actor_id, self.pm.pk)

    def test_override_requires_a_reason(self):
        with self.assertRaises(ValueError):
            with transaction.atomic():
                reserve_headroom(
                    boq_line_id=self.line.pk,
                    qty=Decimal("500"),
                    document_type="purchase_order",
                    document_id=1,
                    actor=self.actor,
                    reason="x",
                    override_actor=self.pm,
                    override_reason="   ",
                )
