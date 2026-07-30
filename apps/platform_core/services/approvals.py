"""Approval engine service layer.

A document declares its type and its value; the engine resolves which rules
apply, raises requests, and locks the document once every rule is satisfied.
"""

from django.db import transaction
from django.utils import timezone

from ..exceptions import DomainError
from ..models import ApprovalAction, ApprovalRequest, ApprovalRule


def applicable_rules(document_type: str, value=None):
    return [
        rule
        for rule in ApprovalRule.objects.filter(document_type=document_type, is_active=True)
        if rule.applies_to(value)
    ]


@transaction.atomic
def request_approval(*, document_type: str, document_id: int, value=None) -> list[ApprovalRequest]:
    """Open an approval request per applicable rule."""
    requests = []
    for rule in applicable_rules(document_type, value):
        requests.append(
            ApprovalRequest.objects.create(
                document_type=document_type, document_id=document_id, rule=rule
            )
        )
    return requests


def _user_holds_role(user, role) -> bool:
    if getattr(user, "is_administrator", False):
        return True
    return user.user_roles.filter(role_id=role.pk).exists()


@transaction.atomic
def act(*, request: ApprovalRequest, actor, action: str, comments: str = "") -> ApprovalAction:
    """Record an approval decision, enforcing that the actor holds the role."""
    if request.status != ApprovalRequest.PENDING:
        raise DomainError(f"Approval request {request.pk} is already {request.status}.")
    if not _user_holds_role(actor, request.rule.required_role):
        raise DomainError(
            f"{actor} does not hold {request.rule.required_role} and cannot "
            f"{action} this {request.document_type}."
        )

    record = ApprovalAction.objects.create(
        request=request, actor=actor, action=action, comments=comments
    )
    request.status = {
        ApprovalAction.APPROVE: ApprovalRequest.APPROVED,
        ApprovalAction.REJECT: ApprovalRequest.REJECTED,
        ApprovalAction.SEND_BACK: ApprovalRequest.SENT_BACK,
    }[action]
    request.resolved_at = timezone.now()
    request.save(update_fields=["status", "resolved_at"])
    return record


def is_fully_approved(*, document_type: str, document_id: int) -> bool:
    requests = ApprovalRequest.objects.filter(
        document_type=document_type, document_id=document_id
    )
    if not requests.exists():
        return False
    return not requests.exclude(status=ApprovalRequest.APPROVED).exists()


@transaction.atomic
def finalize(*, document, actor):
    """Lock a document once every approval on it is in place."""
    document_type = document.__class__.__name__.lower()
    if not is_fully_approved(document_type=document_type, document_id=document.pk):
        raise DomainError(
            f"{document_type} {document.pk} still has outstanding approvals."
        )
    document.lock(actor)
    return document
