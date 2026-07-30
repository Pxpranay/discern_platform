"""Lead assignment and pipeline transitions."""

from django.db import transaction
from django.utils import timezone

from apps.platform_core.exceptions import DomainError
from apps.platform_core.services import events

from .models import Lead, LeadAssignmentRule, Opportunity


def assign_lead(lead: Lead):
    """First matching rule by sequence wins; a blank rule is the catch-all.

    A lead that matches nothing stays unassigned rather than silently going to
    whoever happens to be first in the table.
    """
    for rule in LeadAssignmentRule.objects.filter(is_active=True).order_by("sequence"):
        if rule.matches(lead):
            lead.assigned_to = rule.assign_to
            lead.stage = Lead.ASSIGNED
            lead.save(update_fields=["assigned_to", "stage"])
            return rule.assign_to
    return None


@transaction.atomic
def mark_won(*, opportunity: Opportunity, actor) -> Opportunity:
    if not opportunity.is_open:
        raise DomainError(f"Opportunity {opportunity} is already {opportunity.stage}.")
    opportunity.stage = Opportunity.WON
    opportunity.won_at = timezone.now()
    opportunity.save(update_fields=["stage", "won_at"])
    events.emit(
        "OpportunityWon",
        {"opportunity_id": opportunity.pk},
        idempotency_key=f"OpportunityWon:{opportunity.pk}",
    )
    return opportunity


@transaction.atomic
def mark_lost(*, opportunity: Opportunity, reason: str, actor) -> Opportunity:
    if not opportunity.is_open:
        raise DomainError(f"Opportunity {opportunity} is already {opportunity.stage}.")
    if not reason.strip():
        raise DomainError("A lost opportunity must record why.")
    opportunity.stage = Opportunity.LOST
    opportunity.lost_at = timezone.now()
    opportunity.lost_reason = reason
    opportunity.save(update_fields=["stage", "lost_at", "lost_reason"])
    return opportunity
