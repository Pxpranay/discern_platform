"""Transactional outbox and event delivery.

Design principle 5: hand-offs are events, and events are durable. The earlier
design's automation rules had no answer for a hand-off that fails halfway; at
nineteen hand-offs, some will.
"""

from django.db import transaction
from django.test import TestCase, TransactionTestCase

from apps.platform_core.exceptions import NotInTransaction
from apps.platform_core.models import OutboxEvent
from apps.platform_core.services import events


class TransactionGuardTests(TransactionTestCase):
    """Must not be a ``TestCase``.

    ``TestCase`` wraps every test in an atomic block, so ``in_atomic_block`` is
    always true there and the guard can never be observed to fire. Testing this
    under ``TestCase`` would produce a test that passes whether or not the
    guard exists.
    """

    def test_emit_requires_a_transaction(self):
        """Without one, the event could commit while the change rolls back."""
        with self.assertRaises(NotInTransaction):
            events.emit("Thing.Happened", {"id": 1})

    def test_reserve_headroom_requires_a_transaction(self):
        """The ceiling's row lock is meaningless outside the caller's
        transaction, so the service refuses rather than pretending to hold."""
        from decimal import Decimal

        from apps.platform_core.services.ceiling import reserve_headroom

        from .factories import make_boq_line, make_user

        actor = make_user()
        line = make_boq_line(quantity=Decimal("100"))
        with self.assertRaises(NotInTransaction):
            reserve_headroom(
                boq_line_id=line.pk,
                qty=Decimal("1"),
                document_type="purchase_order",
                document_id=1,
                actor=actor,
                reason="no transaction",
            )


class OutboxTests(TestCase):
    """Each test registers throwaway handlers into an isolated registry, so it
    cannot evict the application's own startup-registered handlers."""

    def setUp(self):
        self._handlers = events.isolated_handlers()
        self._handlers.__enter__()
        self.seen = []

    def tearDown(self):
        self._handlers.__exit__(None, None, None)

    def test_an_event_rolls_back_with_the_change_that_raised_it(self):
        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with transaction.atomic():
                events.emit("Thing.Happened", {"id": 1})
                raise Boom()

        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_a_committed_event_is_delivered_to_its_handler(self):
        @events.handles("OrderConfirmed")
        def handler(payload):
            self.seen.append(payload)

        with transaction.atomic():
            events.emit("OrderConfirmed", {"order_id": 7})

        result = events.drain()
        self.assertEqual(result, {"processed": 1, "failed": 0})
        self.assertEqual(self.seen, [{"order_id": 7}])

        event = OutboxEvent.objects.get()
        self.assertEqual(event.status, OutboxEvent.PROCESSED)
        self.assertIsNotNone(event.processed_at)

    def test_every_handler_for_an_event_runs(self):
        @events.handles("BoqRevisionReleased")
        def reconcile(payload):
            self.seen.append("reconcile")

        @events.handles("BoqRevisionReleased")
        def notify(payload):
            self.seen.append("notify")

        with transaction.atomic():
            events.emit("BoqRevisionReleased", {"revision_id": 1})
        events.drain()
        self.assertEqual(sorted(self.seen), ["notify", "reconcile"])

    def test_an_event_with_no_handler_is_still_marked_processed(self):
        with transaction.atomic():
            events.emit("NobodyCares", {})
        events.drain()
        self.assertEqual(OutboxEvent.objects.get().status, OutboxEvent.PROCESSED)

    def test_a_delivered_event_is_not_delivered_again(self):
        @events.handles("OrderConfirmed")
        def handler(payload):
            self.seen.append(payload)

        with transaction.atomic():
            events.emit("OrderConfirmed", {"order_id": 7})
        events.drain()
        events.drain()
        self.assertEqual(len(self.seen), 1)

    def test_a_failing_handler_marks_the_event_for_retry(self):
        @events.handles("Flaky")
        def handler(payload):
            raise RuntimeError("downstream unavailable")

        with transaction.atomic():
            events.emit("Flaky", {})
        result = events.drain()

        self.assertEqual(result, {"processed": 0, "failed": 1})
        event = OutboxEvent.objects.get()
        self.assertEqual(event.status, OutboxEvent.FAILED)
        self.assertEqual(event.attempts, 1)
        self.assertIn("downstream unavailable", event.last_error)

    def test_a_retried_event_succeeds_once_the_cause_is_fixed(self):
        state = {"fail": True}

        @events.handles("Flaky")
        def handler(payload):
            if state["fail"]:
                raise RuntimeError("not yet")
            self.seen.append("ok")

        with transaction.atomic():
            events.emit("Flaky", {})
        events.drain()
        self.assertEqual(OutboxEvent.objects.get().status, OutboxEvent.FAILED)

        state["fail"] = False
        events.drain()
        self.assertEqual(OutboxEvent.objects.get().status, OutboxEvent.PROCESSED)
        self.assertEqual(self.seen, ["ok"])

    def test_an_event_is_dead_lettered_after_exhausting_its_retries(self):
        @events.handles("AlwaysFails")
        def handler(payload):
            raise RuntimeError("permanent")

        with transaction.atomic():
            events.emit("AlwaysFails", {})

        for _ in range(10):
            events.drain()

        event = OutboxEvent.objects.get()
        self.assertEqual(event.status, OutboxEvent.DEAD)
        self.assertEqual(list(events.dead_letters()), [event])
        # A dead event stops consuming retry attempts.
        self.assertEqual(event.attempts, 5)

    def test_an_administrator_can_replay_a_dead_letter(self):
        state = {"fail": True}

        @events.handles("Recoverable")
        def handler(payload):
            if state["fail"]:
                raise RuntimeError("broken")
            self.seen.append("recovered")

        with transaction.atomic():
            events.emit("Recoverable", {})
        for _ in range(10):
            events.drain()
        self.assertEqual(OutboxEvent.objects.get().status, OutboxEvent.DEAD)

        state["fail"] = False
        events.replay(OutboxEvent.objects.get())

        self.assertEqual(OutboxEvent.objects.get().status, OutboxEvent.PROCESSED)
        self.assertEqual(self.seen, ["recovered"])

    def test_one_failing_handler_does_not_stall_other_events(self):
        @events.handles("Good")
        def good(payload):
            self.seen.append("good")

        @events.handles("Bad")
        def bad(payload):
            raise RuntimeError("nope")

        with transaction.atomic():
            events.emit("Bad", {})
            events.emit("Good", {})

        result = events.drain()
        self.assertEqual(result, {"processed": 1, "failed": 1})
        self.assertEqual(self.seen, ["good"])

    def test_a_partially_failing_handler_chain_rolls_back_its_own_writes(self):
        """Handlers share one transaction, so a later failure undoes earlier
        writes rather than leaving the event half-applied."""
        from apps.platform_core.models import Notification

        from .factories import make_user

        user = make_user()

        @events.handles("HalfApplied")
        def first(payload):
            Notification.objects.create(user=user, title="should not survive")

        @events.handles("HalfApplied")
        def second(payload):
            raise RuntimeError("fails after the first handler wrote")

        with transaction.atomic():
            events.emit("HalfApplied", {})
        events.drain()

        self.assertEqual(Notification.objects.count(), 0)
        self.assertEqual(OutboxEvent.objects.get().status, OutboxEvent.FAILED)
