"""BOQ revision lifecycle: prepare, sign off, release, reconcile."""

from django.db import transaction
from django.utils import timezone

from apps.core.models import BoqLine
from apps.platform_core.exceptions import DomainError
from apps.platform_core.services import events

from . import reconciliation
from .models import BoqRevision, BoqSection, Discipline


@transaction.atomic
def open_revision(*, project, actor=None, copy_forward: bool = True) -> BoqRevision:
    """Start the next revision, optionally copying the last released one forward.

    Copying forward is what establishes ``previous_line``, and therefore what
    lets the diff know a line's identity without having to guess from its text.
    """
    if BoqRevision.objects.filter(
        project=project, status__in=[BoqRevision.DRAFT, BoqRevision.SECTIONS_SIGNED]
    ).exists():
        raise DomainError(
            f"Project {project.code} already has an open BOQ revision. "
            f"Release or send it back before starting another."
        )

    latest = BoqRevision.objects.filter(project=project).order_by("-revision_number").first()
    last_released = (
        BoqRevision.objects.filter(project=project, status=BoqRevision.RELEASED)
        .order_by("-revision_number")
        .first()
    )

    revision = BoqRevision.objects.create(
        project=project,
        revision_number=(latest.revision_number + 1) if latest else 0,
        supersedes=last_released,
    )
    sections = {
        d: BoqSection.objects.create(revision=revision, discipline=d)
        for d in (Discipline.GOODS, Discipline.SERVICE)
    }

    if copy_forward and last_released is not None:
        for line in BoqLine.objects.filter(section__revision=last_released).select_related(
            "section"
        ):
            BoqLine.objects.create(
                project=line.project,
                section=sections[line.section.discipline],
                lot=line.lot,
                item=line.item,
                sl_no=line.sl_no,
                description=line.description,
                quantity=line.quantity,
                uom=line.uom,
                route=line.route,
                drawing_reference=line.drawing_reference,
                previous_line=line,
            )
    return revision


@transaction.atomic
def sign_off_section(*, section: BoqSection, actor) -> BoqSection:
    if section.revision.status not in (BoqRevision.DRAFT, BoqRevision.SECTIONS_SIGNED):
        raise DomainError(f"Revision is {section.revision.status}; nothing to sign off.")
    if section.is_not_applicable:
        raise DomainError("Section is marked not applicable and needs no sign-off.")
    if not section.lines.exists():
        raise DomainError(
            "An empty section cannot be signed off. Mark it not applicable if the "
            "project genuinely has no scope of this discipline."
        )

    section.signed_off_by = actor
    section.signed_off_at = timezone.now()
    section.save(update_fields=["signed_off_by", "signed_off_at"])
    _advance_if_sections_complete(section.revision)
    return section


@transaction.atomic
def mark_section_not_applicable(*, section: BoqSection, actor) -> BoqSection:
    """A materials-only or labour-only project marks the empty side aside.

    This is what stops the release deadlocking on a signature nobody can
    meaningfully give — the failure the earlier two-document design had.
    """
    if section.lines.exists():
        raise DomainError(
            "Section has lines and cannot be marked not applicable. Remove them first."
        )
    section.is_not_applicable = True
    section.signed_off_by = actor
    section.signed_off_at = timezone.now()
    section.save(update_fields=["is_not_applicable", "signed_off_by", "signed_off_at"])
    _advance_if_sections_complete(section.revision)
    return section


def _advance_if_sections_complete(revision: BoqRevision) -> None:
    if all(s.is_complete for s in revision.sections.all()):
        revision.status = BoqRevision.SECTIONS_SIGNED
        revision.save(update_fields=["status"])


@transaction.atomic
def send_back(*, revision: BoqRevision, reason: str, actor) -> BoqRevision:
    """Return a revision to preparation. Nothing downstream has moved."""
    if revision.is_released:
        raise DomainError("A released revision cannot be sent back; issue a new one.")
    if not reason.strip():
        raise DomainError("Sending a revision back requires a reason.")

    revision.status = BoqRevision.DRAFT
    revision.sent_back_reason = reason
    for section in revision.sections.all():
        section.signed_off_by = None
        section.signed_off_at = None
        section.save(update_fields=["signed_off_by", "signed_off_at"])
    revision.save(update_fields=["status", "sent_back_reason"])
    return revision


@transaction.atomic
def release_revision(*, revision: BoqRevision, actor) -> BoqRevision:
    """The Project Manager's single approval that releases the revision.

    Reachable only once both sections are complete — signed off, or explicitly
    not applicable.
    """
    if revision.is_released:
        raise DomainError(f"{revision} is already released.")
    if not actor.has_capability("boq_revision:release"):
        raise DomainError(f"{actor} cannot release a BOQ revision.")

    incomplete = [s for s in revision.sections.all() if not s.is_complete]
    if incomplete:
        names = ", ".join(s.get_discipline_display() for s in incomplete)
        raise DomainError(
            f"Cannot release: {names} section not signed off. "
            f"Mark it not applicable if the project has no such scope."
        )

    revision.status = BoqRevision.RELEASED
    revision.released_by = actor
    revision.released_at = timezone.now()
    revision.save(update_fields=["status", "released_by", "released_at"])
    revision.lock(actor)

    events.emit(
        "BoqRevisionReleased",
        {"revision_id": revision.pk, "project_id": revision.project_id},
        idempotency_key=f"BoqRevisionReleased:{revision.pk}",
    )
    return revision


@events.handles("BoqRevisionReleased")
def _reconcile_on_release(payload: dict) -> None:
    """Released revisions reconcile automatically — the hand-off to Procurement."""
    revision = BoqRevision.objects.get(pk=payload["revision_id"])
    if revision.reconciliation_outcomes.exists():
        return  # delivery is at-least-once
    reconciliation.persist(reconciliation.reconcile(revision))
