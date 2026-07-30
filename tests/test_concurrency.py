"""The ceiling under genuine concurrency.

The earlier design validated the ceiling separately on three document types by
summing order lines and comparing. That has a race: two buyers confirming at
the same moment both read the same total, both find room, and both pass —
leaving the line over-committed with no rule broken from either side's view.

These tests use real threads on real connections, so the row lock is actually
exercised. ``TransactionTestCase`` is required: the default ``TestCase`` wraps
each test in a transaction that other connections cannot see into.
"""

import threading
from decimal import Decimal

from django.db import connection, transaction
from django.test import TransactionTestCase

from apps.platform_core.exceptions import CeilingExceeded
from apps.platform_core.services.ceiling import committed_qty

from .factories import make_boq_line, make_user


def _reserve_in_thread(boq_line_id, qty, doc_id, actor_id, barrier, results, index):
    """Run one reservation on this thread's own database connection."""
    from apps.accounts.models import AppUser
    from apps.platform_core.services.ceiling import reserve_headroom

    try:
        actor = AppUser.objects.get(pk=actor_id)
        barrier.wait(timeout=10)  # maximize the overlap
        with transaction.atomic():
            reserve_headroom(
                boq_line_id=boq_line_id,
                qty=Decimal(qty),
                document_type="purchase_order",
                document_id=doc_id,
                actor=actor,
                reason="concurrent test",
            )
        results[index] = "ok"
    except CeilingExceeded:
        results[index] = "blocked"
    except Exception as exc:  # pragma: no cover - surfaces unexpected failures
        results[index] = f"error: {exc!r}"
    finally:
        connection.close()


class CeilingConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_two_simultaneous_reservations_cannot_both_take_the_same_headroom(self):
        """Ceiling 100, two threads each asking for 60. Exactly one wins."""
        actor = make_user()
        line = make_boq_line(quantity=Decimal("100"))

        barrier = threading.Barrier(2)
        results: dict[int, str] = {}
        threads = [
            threading.Thread(
                target=_reserve_in_thread,
                args=(line.pk, 60, i + 1, actor.pk, barrier, results, i),
            )
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        outcomes = sorted(results.values())
        self.assertEqual(
            outcomes,
            ["blocked", "ok"],
            f"expected exactly one success and one block, got {results}",
        )
        self.assertEqual(committed_qty(line.pk), Decimal("60"))

    def test_many_simultaneous_reservations_never_exceed_the_ceiling(self):
        """Ten threads each asking for 15 against a ceiling of 100.

        Six should fit (90), the rest must be blocked. Whatever the interleaving,
        the committed total must never end up above the ceiling.
        """
        actor = make_user()
        line = make_boq_line(quantity=Decimal("100"))

        n = 10
        barrier = threading.Barrier(n)
        results: dict[int, str] = {}
        threads = [
            threading.Thread(
                target=_reserve_in_thread,
                args=(line.pk, 15, i + 1, actor.pk, barrier, results, i),
            )
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        errors = [r for r in results.values() if r.startswith("error")]
        self.assertEqual(errors, [], f"unexpected failures: {errors}")

        succeeded = sum(1 for r in results.values() if r == "ok")
        self.assertEqual(succeeded, 6, f"expected 6 to fit within 100, got {results}")
        self.assertEqual(committed_qty(line.pk), Decimal("90"))
        self.assertLessEqual(committed_qty(line.pk), Decimal("100"))
