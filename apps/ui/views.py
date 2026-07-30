"""Screens.

Server-rendered, deliberately plain. Every action goes through the same domain
services the tests exercise — a view never writes a model directly, so the
quantity ceiling, the schedule cap and lock-on-approval hold here exactly as
they do everywhere else.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from apps.core.models import BoqLine, Location, Project
from apps.crm.models import Activity, Lead, Opportunity
from apps.engineering import services as boq_services
from apps.engineering.models import BoqRevision, BoqSection, ReconciliationOutcome
from apps.platform_core.exceptions import DomainError
from apps.platform_core.models import OutboxEvent
from apps.platform_core.services import costing, events
from apps.platform_core.services.ceiling import committed_qty, headroom
from apps.projects import services as project_services
from apps.projects.models import SchedulePhase
from apps.sales import services as sales_services
from apps.sales.models import Client, Lot, Order

from .access import require_action, requires


def _decimal(raw, field):
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise DomainError(f"{field} must be a number.")


def _date(raw, field):
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise DomainError(f"{field} must be a date in YYYY-MM-DD form.")


# --------------------------------------------------------------- dashboard
@requires("dashboard:view")
def dashboard(request):
    projects = Project.objects.select_related("client", "order").order_by("-created_at")
    rows = []
    for project in projects[:25]:
        result = costing.profitability(project.pk)
        latest = project.boq_revisions.filter(status=BoqRevision.RELEASED).first()
        rows.append(
            {
                "project": project,
                "revenue": result["revenue"],
                "cost": result["cost"],
                "margin": result["margin"],
                "pct": (result["margin"] / result["revenue"] * 100)
                if result["revenue"]
                else None,
                "boq": latest,
                "overdue": project.schedule_phases.filter(
                    is_complete=False, planned_end__lt=date.today()
                ).count(),
            }
        )

    revenue = sum((r["revenue"] for r in rows), Decimal("0"))
    cost = sum((r["cost"] for r in rows), Decimal("0"))

    return render(
        request,
        "ui/dashboard.html",
        {
            "rows": rows,
            "revenue": revenue,
            "cost": cost,
            "margin": revenue - cost,
            "open_orders": Order.objects.filter(status=Order.HELD).count(),
            "open_leads": Lead.objects.exclude(stage=Lead.REJECTED).count(),
            "dead_letters": OutboxEvent.objects.filter(status=OutboxEvent.DEAD).count(),
            "nav": "dashboard",
        },
    )


# --------------------------------------------------------------------- CRM
@requires("crm:view")
def crm(request):
    return render(
        request,
        "ui/crm/index.html",
        {
            "leads": Lead.objects.select_related("assigned_to").order_by("-created_at")[:50],
            "opportunities": Opportunity.objects.select_related("client", "owner")
            .order_by("-created_at")[:50],
            "activities": Activity.objects.select_related("opportunity", "assigned_to")
            .filter(completed_at__isnull=True)
            .order_by("due_date")[:20],
            "stages": Opportunity.STAGE_CHOICES,
            "nav": "crm",
        },
    )


# ------------------------------------------------------------------- sales
@requires("sales:view")
def orders(request):
    return render(
        request,
        "ui/sales/orders.html",
        {
            "orders": Order.objects.select_related("client")
            .annotate(lot_count=Count("lots"))
            .order_by("-created_at")[:50],
            "clients": Client.objects.order_by("name")[:100],
            "nav": "sales",
        },
    )


@requires("sales:view")
def order_detail(request, pk):
    order = get_object_or_404(Order.objects.select_related("client"), pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "confirm":
                require_action(request, "order:confirm")
                sales_services.confirm_order(
                    order=order,
                    committed_delivery_date=_date(
                        request.POST.get("committed_delivery_date"), "Committed delivery date"
                    ),
                    actor=request.user,
                )
                messages.success(request, "Order confirmed. It now waits at the kickoff gate.")
            elif action == "kickoff":
                require_action(request, "order:approve_kickoff")
                sales_services.approve_for_kickoff(order=order, actor=request.user)
                events.drain()
                messages.success(request, "Approved for kickoff — the project has been created.")
            elif action == "hold":
                sales_services.hold_for_review(
                    order=order, reason=request.POST.get("reason", ""), actor=request.user
                )
                messages.info(request, "Order held for re-review.")
        except DomainError as exc:
            messages.error(request, str(exc))
        return redirect("order_detail", pk=order.pk)

    return render(
        request,
        "ui/sales/order_detail.html",
        {
            "order": order,
            "lots": order.lots.select_related("project").order_by("sequence"),
            "project": Project.objects.filter(order=order).first(),
            "invoices": order.invoices.select_related("lot").order_by("-invoice_date"),
            "nav": "sales",
        },
    )


# ---------------------------------------------------------------- projects
@requires("projects:view")
def projects(request):
    return render(
        request,
        "ui/projects/list.html",
        {
            "projects": Project.objects.select_related("client")
            .annotate(phases=Count("schedule_phases", distinct=True))
            .order_by("-created_at")[:50],
            "nav": "projects",
        },
    )


@requires("projects:view")
def project_detail(request, pk):
    project = get_object_or_404(
        Project.objects.select_related("client", "order"), pk=pk
    )

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "plan_phase":
                require_action(request, "project:plan_schedule")
                project_services.plan_phase(
                    project=project,
                    name=request.POST.get("name", "").strip() or "Untitled phase",
                    kind=request.POST.get("kind"),
                    planned_end=_date(request.POST.get("planned_end"), "Target date"),
                    sequence=int(request.POST.get("sequence") or 1),
                    actor=request.user,
                )
                messages.success(request, "Phase added.")
            elif action == "extend":
                project_services.extend_commitment(
                    project=project,
                    new_committed_date=_date(
                        request.POST.get("new_committed_date"), "New committed date"
                    ),
                    client_agreement_reference=request.POST.get("reference", ""),
                    actor=request.user,
                )
                messages.success(request, "Committed delivery date extended.")
            elif action == "open_revision":
                require_action(request, "boq:edit")
                revision = boq_services.open_revision(project=project, actor=request.user)
                messages.success(request, f"BOQ Rev {revision.revision_number} opened.")
                return redirect("boq_detail", pk=revision.pk)
        except DomainError as exc:
            messages.error(request, str(exc))
        return redirect("project_detail", pk=project.pk)

    result = costing.profitability(project.pk)
    return render(
        request,
        "ui/projects/detail.html",
        {
            "project": project,
            "lots": project.lots.order_by("sequence"),
            "phases": project.schedule_phases.order_by("sequence"),
            "extensions": project.schedule_extensions.all(),
            "revisions": project.boq_revisions.annotate(
                line_count=Count("sections__lines")
            ),
            "locations": Location.objects.filter(project=project),
            "profit": result,
            "lot_profit": [
                (lot, costing.lot_profitability(lot.pk)) for lot in project.lots.all()
            ],
            "phase_kinds": SchedulePhase.KIND_CHOICES,
            "today": date.today(),
            "nav": "projects",
        },
    )


# --------------------------------------------------------------------- BOQ
@requires("boq:view")
def boq_list(request):
    return render(
        request,
        "ui/boq/list.html",
        {
            "revisions": BoqRevision.objects.select_related("project", "released_by")
            .annotate(line_count=Count("sections__lines"))
            .order_by("-prepared_at")[:50],
            "nav": "boq",
        },
    )


@requires("boq:view")
def boq_detail(request, pk):
    revision = get_object_or_404(
        BoqRevision.objects.select_related("project", "released_by"), pk=pk
    )

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "sign_off":
                require_action(request, "boq:sign_off")
                section = get_object_or_404(
                    BoqSection, pk=request.POST.get("section_id"), revision=revision
                )
                boq_services.sign_off_section(section=section, actor=request.user)
                messages.success(request, f"{section.get_discipline_display()} section signed off.")
            elif action == "not_applicable":
                require_action(request, "boq:sign_off")
                section = get_object_or_404(
                    BoqSection, pk=request.POST.get("section_id"), revision=revision
                )
                boq_services.mark_section_not_applicable(section=section, actor=request.user)
                messages.success(request, "Section marked not applicable.")
            elif action == "release":
                require_action(request, "boq_revision:release")
                boq_services.release_revision(revision=revision, actor=request.user)
                events.drain()
                messages.success(request, "Revision released. Reconciliation has run.")
            elif action == "send_back":
                require_action(request, "boq_revision:release")
                boq_services.send_back(
                    revision=revision, reason=request.POST.get("reason", ""), actor=request.user
                )
                messages.info(request, "Revision sent back for revision.")
            elif action == "add_line":
                require_action(request, "boq:edit")
                section = get_object_or_404(
                    BoqSection, pk=request.POST.get("section_id"), revision=revision
                )
                if revision.is_released:
                    raise DomainError("A released revision cannot be changed. Open a new one.")
                BoqLine.objects.create(
                    project=revision.project,
                    section=section,
                    lot_id=request.POST.get("lot") or None,
                    sl_no=request.POST.get("sl_no", "").strip(),
                    description=request.POST.get("description", "").strip(),
                    quantity=_decimal(request.POST.get("quantity"), "Quantity"),
                    uom=request.POST.get("uom", "nos").strip() or "nos",
                    route=request.POST.get("route") or BoqLine.SUPPLY,
                )
                messages.success(request, "Line added.")
        except DomainError as exc:
            messages.error(request, str(exc))
        return redirect("boq_detail", pk=revision.pk)

    sections = []
    for section in revision.sections.order_by("discipline"):
        lines = []
        # Insertion order is the document's own order. Ordering by sl_no
        # sorts it as text, putting SL 10 between 1 and 2.
        for line in section.lines.select_related("lot").order_by("id"):
            lines.append(
                {
                    "line": line,
                    "committed": committed_qty(line.pk),
                    "headroom": headroom(line.pk),
                }
            )
        sections.append({"section": section, "lines": lines})

    return render(
        request,
        "ui/boq/detail.html",
        {
            "revision": revision,
            "sections": sections,
            "outcomes": revision.reconciliation_outcomes.all(),
            "acting": revision.reconciliation_outcomes.exclude(
                action=ReconciliationOutcome.NONE
            ),
            "lots": revision.project.lots.order_by("sequence"),
            "routes": BoqLine.ROUTE_CHOICES,
            "can_release": all(s["section"].is_complete for s in sections)
            and not revision.is_released,
            "nav": "boq",
        },
    )
