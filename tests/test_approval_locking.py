"""Approval engine and lock-on-approval governance.

Process design §5.2: free to edit until approved, locked the moment approval
fires, Administrator-only thereafter — with every override logged and visible
to the Directors.

The important claim these tests defend is that locking holds on *every* write
path, not just the one the UI happens to use. A lock that holds in the API and
not in the admin screen is not a lock.
"""

from decimal import Decimal

from django.test import TestCase

from apps.platform_core.exceptions import DomainError, MissingReason, RecordLocked
from apps.platform_core.models import (
    AdminOverride,
    ApprovalAction,
    ApprovalRequest,
    ApprovalRule,
    Override,
)
from apps.platform_core.services import approvals
from apps.testsupport.models import DemoDocument

from .factories import make_role, make_user


class RecordLockingTests(TestCase):
    def setUp(self):
        self.actor = make_user()
        self.admin = make_user(username="admin", is_administrator=True)
        self.doc = DemoDocument.objects.create(name="draft", value=Decimal("100"))

    def test_an_unapproved_document_is_freely_editable(self):
        self.doc.name = "revised"
        self.doc.save()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.name, "revised")

    def test_locking_on_approval_blocks_further_edits(self):
        self.doc.lock(self.actor)
        self.doc.name = "sneaky edit"
        with self.assertRaises(RecordLocked):
            self.doc.save()

    def test_locking_blocks_the_bulk_update_path(self):
        """``QuerySet.update`` never calls ``save``, so it needs its own guard."""
        self.doc.lock(self.actor)
        with self.assertRaises(RecordLocked):
            DemoDocument.objects.filter(pk=self.doc.pk).update(name="bulk edit")
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.name, "draft")

    def test_locking_blocks_deletion(self):
        self.doc.lock(self.actor)
        with self.assertRaises(RecordLocked):
            self.doc.delete()

    def test_an_administrator_override_is_permitted_and_recorded(self):
        self.doc.lock(self.actor)
        self.doc.name = "corrected after approval"

        override = Override(self.admin, reason="wrong item code, agreed with PM")
        self.doc.save(override=override)

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.name, "corrected after approval")

        logged = AdminOverride.objects.get(entity_id=self.doc.pk)
        self.assertEqual(logged.actor, self.admin)
        self.assertEqual(logged.reason, "wrong item code, agreed with PM")
        self.assertEqual(logged.before["name"], "draft")
        self.assertEqual(logged.after["name"], "corrected after approval")

    def test_a_non_administrator_cannot_construct_an_override(self):
        with self.assertRaises(DomainError):
            Override(self.actor, reason="let me through")

    def test_an_override_requires_a_reason(self):
        with self.assertRaises(MissingReason):
            Override(self.admin, reason="   ")

    def test_overrides_are_listable_for_director_review(self):
        """They are permitted, but never quiet."""
        for i in range(3):
            doc = DemoDocument.objects.create(name=f"doc{i}", value=Decimal("1"))
            doc.lock(self.actor)
            doc.name = f"changed{i}"
            doc.save(override=Override(self.admin, reason=f"reason {i}"))
        self.assertEqual(AdminOverride.objects.count(), 3)


class ApprovalEngineTests(TestCase):
    def setUp(self):
        self.buyer = make_user(username="buyer")
        self.manager = make_user(username="purchase_manager")
        self.manager_role = make_role(
            code="purchase_manager", capabilities=["purchase_order:approve"]
        )
        self.manager.user_roles.create(role=self.manager_role)

        ApprovalRule.objects.create(
            document_type="demodocument",
            name="Purchase Manager above threshold",
            threshold=Decimal("500000"),
            required_role=self.manager_role,
        )

    def test_a_document_below_the_threshold_needs_no_approval(self):
        requests = approvals.request_approval(
            document_type="demodocument", document_id=1, value=Decimal("100000")
        )
        self.assertEqual(requests, [])

    def test_a_document_above_the_threshold_raises_a_request(self):
        requests = approvals.request_approval(
            document_type="demodocument", document_id=1, value=Decimal("800000")
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].status, ApprovalRequest.PENDING)

    def test_only_the_required_role_can_approve(self):
        request = approvals.request_approval(
            document_type="demodocument", document_id=1, value=Decimal("800000")
        )[0]
        with self.assertRaises(DomainError):
            approvals.act(request=request, actor=self.buyer, action=ApprovalAction.APPROVE)

    def test_the_required_role_can_approve(self):
        request = approvals.request_approval(
            document_type="demodocument", document_id=1, value=Decimal("800000")
        )[0]
        approvals.act(request=request, actor=self.manager, action=ApprovalAction.APPROVE)
        request.refresh_from_db()
        self.assertEqual(request.status, ApprovalRequest.APPROVED)
        self.assertTrue(
            approvals.is_fully_approved(document_type="demodocument", document_id=1)
        )

    def test_a_rejected_request_leaves_the_document_unapproved(self):
        request = approvals.request_approval(
            document_type="demodocument", document_id=1, value=Decimal("800000")
        )[0]
        approvals.act(request=request, actor=self.manager, action=ApprovalAction.REJECT)
        self.assertFalse(
            approvals.is_fully_approved(document_type="demodocument", document_id=1)
        )

    def test_a_request_cannot_be_actioned_twice(self):
        request = approvals.request_approval(
            document_type="demodocument", document_id=1, value=Decimal("800000")
        )[0]
        approvals.act(request=request, actor=self.manager, action=ApprovalAction.APPROVE)
        with self.assertRaises(DomainError):
            approvals.act(
                request=request, actor=self.manager, action=ApprovalAction.APPROVE
            )

    def test_finalize_locks_a_fully_approved_document(self):
        doc = DemoDocument.objects.create(name="big order", value=Decimal("800000"))
        request = approvals.request_approval(
            document_type="demodocument", document_id=doc.pk, value=doc.value
        )[0]
        approvals.act(request=request, actor=self.manager, action=ApprovalAction.APPROVE)

        approvals.finalize(document=doc, actor=self.manager)
        doc.refresh_from_db()
        self.assertTrue(doc.is_locked)

        doc.name = "changed after approval"
        with self.assertRaises(RecordLocked):
            doc.save()

    def test_finalize_refuses_while_an_approval_is_outstanding(self):
        doc = DemoDocument.objects.create(name="big order", value=Decimal("800000"))
        approvals.request_approval(
            document_type="demodocument", document_id=doc.pk, value=doc.value
        )
        with self.assertRaises(DomainError):
            approvals.finalize(document=doc, actor=self.manager)
        doc.refresh_from_db()
        self.assertFalse(doc.is_locked)

    def test_multiple_rules_all_have_to_be_satisfied(self):
        director_role = make_role(code="director")
        director = make_user(username="director")
        director.user_roles.create(role=director_role)
        ApprovalRule.objects.create(
            document_type="demodocument",
            name="Director above a higher threshold",
            threshold=Decimal("700000"),
            required_role=director_role,
            sequence=2,
        )

        doc = DemoDocument.objects.create(name="very big", value=Decimal("800000"))
        requests = approvals.request_approval(
            document_type="demodocument", document_id=doc.pk, value=doc.value
        )
        self.assertEqual(len(requests), 2)

        approvals.act(
            request=requests[0], actor=self.manager, action=ApprovalAction.APPROVE
        )
        self.assertFalse(
            approvals.is_fully_approved(document_type="demodocument", document_id=doc.pk)
        )

        approvals.act(
            request=requests[1], actor=director, action=ApprovalAction.APPROVE
        )
        self.assertTrue(
            approvals.is_fully_approved(document_type="demodocument", document_id=doc.pk)
        )
