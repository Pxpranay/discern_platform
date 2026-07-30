"""Project initiation and the master schedule.

Two things live here. The automatic hand-off from a kicked-off order to a
project — the first place the design removes re-typing — and the schedule
ceiling, which is the one hard boundary on planning.
"""

from django.db import transaction
from django.utils import timezone

from apps.core.models import Location, Project
from apps.platform_core.exceptions import DomainError
from apps.platform_core.services import events
from apps.sales.models import Order

from .models import PhaseDateChange, SchedulePhase, ScheduleExtension


class ScheduleExceedsCommitment(DomainError):
    """A phase was planned beyond the date promised to the client."""

    def __init__(self, planned_end, committed_date):
        self.planned_end = planned_end
        self.committed_date = committed_date
        super().__init__(
            f"Phase would end {planned_end}, beyond the committed delivery date "
            f"{committed_date}. The client must agree an extension before the "
            f"schedule can run past it."
        )


@transaction.atomic
def initiate_project(*, order: Order, code: str | None = None, actor=None) -> Project:
    """Create the project from a kicked-off order.

    Copies the client, the committed date and every lot, and provisions the
    project's own stock location. Nothing here is re-typed by a human — which
    is the entire point of the Sales-to-Project hand-off.
    """
    if order.status != Order.KICKED_OFF:
        raise DomainError(
            f"Order {order.number} is {order.status}; only a kicked-off order "
            f"creates a project."
        )
    existing = Project.objects.filter(order=order).first()
    if existing is not None:
        # The outbox delivers at least once, so this must be idempotent.
        return existing

    code = code or f"P-{order.number}"
    project = Project.objects.create(
        code=code,
        name=f"{order.client.name} — {order.number}",
        order=order,
        client=order.client,
        status=Project.PLANNING,
        budget=order.total_value,
        effective_committed_date=order.committed_delivery_date,
    )

    # Lots carry forward to the project. Every downstream cost traces to one.
    order.lots.update(project=project)

    Location.objects.create(
        code=f"{code}-SITE",
        name=f"{project.name} site",
        kind=Location.SITE,
        project=project,
    )

    events.emit(
        "ProjectInitiated",
        {"project_id": project.pk, "order_id": order.pk},
        idempotency_key=f"ProjectInitiated:{order.pk}",
    )
    return project


@events.handles("OrderApprovedForKickoff")
def _create_project_on_kickoff(payload: dict) -> None:
    """The automatic hand-off. Sales approves; the project appears."""
    order = Order.objects.get(pk=payload["order_id"])
    initiate_project(order=order)


def _committed_date(project: Project):
    if project.effective_committed_date is None:
        raise DomainError(
            f"Project {project.code} has no committed delivery date; the "
            f"schedule has no ceiling to check against."
        )
    return project.effective_committed_date


@transaction.atomic
def plan_phase(
    *,
    project: Project,
    name: str,
    kind: str,
    planned_end,
    planned_start=None,
    sequence: int = 1,
    actor,
) -> SchedulePhase:
    """Add a phase, refusing any date beyond the committed delivery date.

    Enforced here rather than as a database CHECK constraint: the rule spans two
    tables (``schedule_phase`` and ``project``), which a row-level CHECK cannot
    express. Every write path goes through this service.
    """
    committed = _committed_date(project)
    if planned_end > committed:
        raise ScheduleExceedsCommitment(planned_end, committed)

    return SchedulePhase.objects.create(
        project=project,
        name=name,
        kind=kind,
        sequence=sequence,
        planned_start=planned_start,
        planned_end=planned_end,
    )


@transaction.atomic
def reschedule_phase(*, phase: SchedulePhase, new_end, actor, reason: str = "") -> SchedulePhase:
    """Move a phase's target date, logging who moved it and why.

    Every date on the schedule is a live parameter rather than an estimate
    frozen at kickoff — so the change log is what keeps that accountable.
    """
    committed = _committed_date(phase.project)
    if new_end > committed:
        raise ScheduleExceedsCommitment(new_end, committed)

    PhaseDateChange.objects.create(
        phase=phase,
        previous_end=phase.planned_end,
        new_end=new_end,
        changed_by=actor,
        reason=reason,
    )
    phase.planned_end = new_end
    phase.save(update_fields=["planned_end"])
    return phase


@transaction.atomic
def extend_commitment(
    *,
    project: Project,
    new_committed_date,
    client_agreement_reference: str,
    actor,
) -> ScheduleExtension:
    """Raise the schedule ceiling after the client has agreed a later date.

    Restricted to the CEO and Project Manager, and requires a recorded client
    agreement — this is a contractual date, so sign-off alone is not evidence
    the client agreed to move it.
    """
    if not actor.has_capability("project:extend_schedule"):
        raise DomainError(
            f"{actor} cannot extend the committed delivery date; that is the "
            f"CEO's or Project Manager's authority."
        )
    if not client_agreement_reference or not client_agreement_reference.strip():
        raise DomainError(
            "Extending the committed date requires a recorded client agreement."
        )

    previous = _committed_date(project)
    if new_committed_date <= previous:
        raise DomainError(
            f"New committed date {new_committed_date} is not later than the "
            f"current {previous}."
        )

    extension = ScheduleExtension.objects.create(
        project=project,
        previous_committed_date=previous,
        new_committed_date=new_committed_date,
        client_agreement_reference=client_agreement_reference,
        authorized_by=actor,
    )
    project.effective_committed_date = new_committed_date
    project.save(update_fields=["effective_committed_date"])

    events.emit(
        "ScheduleExtended",
        {"project_id": project.pk, "new_date": str(new_committed_date)},
    )
    return extension


def schedule_status(project: Project) -> dict:
    """What the Project Manager's dashboard shows for the schedule."""
    phases = list(project.schedule_phases.all())
    today = timezone.now().date()
    committed = project.effective_committed_date
    overdue = [p for p in phases if not p.is_complete and p.planned_end < today]
    return {
        "committed_date": committed,
        "phases": len(phases),
        "complete": sum(1 for p in phases if p.is_complete),
        "overdue": len(overdue),
        "latest_planned_end": max((p.planned_end for p in phases), default=None),
        "headroom_days": (committed - max((p.planned_end for p in phases), default=today)).days
        if committed and phases
        else None,
    }
