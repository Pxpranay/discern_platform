"""The BOQ revision reconciliation engine.

Process design §4.5. When a revision is released, Procurement must never see
the whole BOQ re-sent. It sees only the *net* change per line, computed against
what has actually been committed and received — read from the commitment and
stock ledgers, not from the previous revision's text.

That distinction matters more than it first appears. Comparing revision to
revision tells you what the engineer changed. Comparing revision to the ledgers
tells you what still needs doing, which is a different number the moment any
order is in flight — and on a live project, one usually is.

Six outcomes, each routed differently:

===========================  ====================================================
Outcome                      Action
===========================  ====================================================
New line                     Request the full quantity
Quantity increased           Request the delta only, never the whole line
Decreased, nothing           Reduce or remove the still-draft request. Quiet:
committed                    no vendor has been contacted, nobody is notified
Decreased, already ordered   Amend or cancel the outstanding order quantity
Decreased, already received  Return / redeployment queue — material exists
Unchanged                    Nothing
===========================  ====================================================
"""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Sum

from apps.core.models import BoqLine
from apps.platform_core.models import CommitmentEntry, StockMove

from .models import BoqRevision, ReconciliationOutcome

ZERO = Decimal("0")


def normalize(text: str | None) -> str:
    """Fold a description or unit to a stable matching key.

    Real BOQ documents are typed by hand and are not internally consistent —
    Discern's own Rev 0 contains both "MS ERW Pipe 80 mm NB" and "MS ERW Pipe
    100 mm Nb" in the same nine-line table. Matching on the raw string would
    read that as two unrelated items across a revision boundary, and produce a
    spurious return plus a spurious purchase for a line nobody touched.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = text.replace(" ", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def match_key(line: BoqLine) -> tuple[str, str]:
    return (normalize(line.description), normalize(line.uom))


def committed_for(boq_line_id: int) -> Decimal:
    total = CommitmentEntry.objects.filter(boq_line_id=boq_line_id).aggregate(
        t=Sum("qty_delta")
    )["t"]
    return total if total is not None else ZERO


def received_for(boq_line_id: int) -> Decimal:
    """Net quantity physically received against this line."""
    inbound = StockMove.objects.filter(
        boq_line_id=boq_line_id, to_location__isnull=False
    ).aggregate(t=Sum("quantity"))["t"] or ZERO
    outbound = StockMove.objects.filter(
        boq_line_id=boq_line_id, from_location__isnull=False
    ).aggregate(t=Sum("quantity"))["t"] or ZERO
    return inbound - outbound


@dataclass
class LineOutcome:
    """One line's verdict. Mirrors ``ReconciliationOutcome`` before persistence."""

    description: str
    uom: str
    previous_qty: Decimal
    new_qty: Decimal
    committed_qty: Decimal
    received_qty: Decimal
    kind: str
    action: str
    boq_line: BoqLine | None = None
    previous_line: BoqLine | None = None
    excess_received: Decimal = ZERO
    order_reduction: Decimal = ZERO

    @property
    def is_removal(self) -> bool:
        """Cut to zero: dropped from the project rather than merely reduced."""
        return self.new_qty == ZERO and self.previous_qty > ZERO

    @property
    def delta(self) -> Decimal:
        return self.new_qty - self.previous_qty


@dataclass
class Reconciliation:
    revision: BoqRevision
    outcomes: list[LineOutcome] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[LineOutcome]:
        return [o for o in self.outcomes if o.kind == kind]

    def requiring_action(self) -> list[LineOutcome]:
        return [o for o in self.outcomes if o.action != ReconciliationOutcome.NONE]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
        return counts


def _index_lines(revision: BoqRevision | None) -> dict[tuple[str, str], BoqLine]:
    if revision is None:
        return {}
    lines = BoqLine.objects.filter(section__revision=revision).select_related("section")
    return {match_key(line): line for line in lines}


def _classify(previous_qty: Decimal, new_qty: Decimal, committed: Decimal, received: Decimal):
    """Decide the outcome kind and the action for one line.

    Returns ``(kind, action, excess_received, order_reduction)``.

    All six cases sit together so the routing can be read as a whole. Every
    branch is exercised by the tests against Discern's real Rev 0 to Rev 1.
    """
    if previous_qty == ZERO and new_qty > ZERO:
        return ReconciliationOutcome.NEW, ReconciliationOutcome.REQUEST_DELTA, ZERO, ZERO

    if new_qty > previous_qty:
        return ReconciliationOutcome.INCREASED, ReconciliationOutcome.REQUEST_DELTA, ZERO, ZERO

    if new_qty == previous_qty:
        return ReconciliationOutcome.UNCHANGED, ReconciliationOutcome.NONE, ZERO, ZERO

    # A decrease. Where it goes depends only on what has already happened
    # against the line — not on whether the cut happens to reach zero.
    if received > new_qty:
        # Material beyond the new requirement is already physically at site.
        return (
            ReconciliationOutcome.DECREASED_RECEIVED,
            ReconciliationOutcome.RETURN_QUEUE,
            received - new_qty,
            max(committed - received, ZERO),
        )

    if committed > new_qty:
        # Ordered but not yet delivered — amend the outstanding quantity down.
        return (
            ReconciliationOutcome.DECREASED_ORDERED,
            ReconciliationOutcome.AMEND_ORDER,
            ZERO,
            committed - new_qty,
        )

    # Nothing committed beyond the new quantity. The quiet outcome: no vendor
    # has been contacted, so nobody is notified and nothing is queued.
    return (
        ReconciliationOutcome.DECREASED_UNCOMMITTED,
        ReconciliationOutcome.REDUCE_DRAFT,
        ZERO,
        ZERO,
    )


def reconcile(revision: BoqRevision) -> Reconciliation:
    """Compute the net change per line for a released revision.

    Line identity comes from ``previous_line`` where the revision was prepared
    by copying a released one forward — the normal in-app path. Imported or
    hand-built revisions fall back to a normalized description-and-unit match.
    """
    previous_revision = (
        BoqRevision.objects.filter(
            project=revision.project,
            status=BoqRevision.RELEASED,
            revision_number__lt=revision.revision_number,
        )
        .order_by("-revision_number")
        .first()
    )

    previous_by_key = _index_lines(previous_revision)
    current_lines = list(
        BoqLine.objects.filter(section__revision=revision).select_related("section")
    )

    result = Reconciliation(revision=revision)
    matched_previous_ids: set[int] = set()

    for line in current_lines:
        previous = line.previous_line
        if previous is None:
            previous = previous_by_key.get(match_key(line))
        if previous is not None:
            matched_previous_ids.add(previous.pk)

        previous_qty = previous.quantity if previous is not None else ZERO
        # Commitment and receipt follow the line's identity across revisions,
        # so a carried-forward line keeps what was already ordered against it.
        ledger_line_id = previous.pk if previous is not None else line.pk
        committed = committed_for(ledger_line_id)
        received = received_for(ledger_line_id)

        kind, action, excess, order_reduction = _classify(
            previous_qty, line.quantity, committed, received
        )

        result.outcomes.append(
            LineOutcome(
                description=line.description,
                uom=line.uom,
                previous_qty=previous_qty,
                new_qty=line.quantity,
                committed_qty=committed,
                received_qty=received,
                kind=kind,
                action=action,
                boq_line=line,
                previous_line=previous,
                excess_received=excess,
                order_reduction=order_reduction,
            )
        )

    # Lines that existed before and are absent entirely from this revision.
    # Discern's own documents zero the quantity instead of deleting the row, so
    # this path handles revisions written the other way.
    for key, previous in previous_by_key.items():
        if previous.pk in matched_previous_ids:
            continue
        committed = committed_for(previous.pk)
        received = received_for(previous.pk)
        kind, action, excess, order_reduction = _classify(
            previous.quantity, ZERO, committed, received
        )
        result.outcomes.append(
            LineOutcome(
                description=previous.description,
                uom=previous.uom,
                previous_qty=previous.quantity,
                new_qty=ZERO,
                committed_qty=committed,
                received_qty=received,
                kind=kind,
                action=action,
                boq_line=None,
                previous_line=previous,
                excess_received=excess,
                order_reduction=order_reduction,
            )
        )

    return result


def persist(reconciliation: Reconciliation) -> list[ReconciliationOutcome]:
    """Store the verdicts. They are Procurement's instructions, and the record
    of what was decided and when."""
    rows = [
        ReconciliationOutcome(
            revision=reconciliation.revision,
            boq_line=o.boq_line,
            previous_line=o.previous_line,
            description=o.description,
            uom=o.uom,
            previous_qty=o.previous_qty,
            new_qty=o.new_qty,
            delta=o.delta,
            committed_qty=o.committed_qty,
            received_qty=o.received_qty,
            excess_received=o.excess_received,
            order_reduction=o.order_reduction,
            kind=o.kind,
            action=o.action,
            is_removal=o.is_removal,
        )
        for o in reconciliation.outcomes
    ]
    return ReconciliationOutcome.objects.bulk_create(rows)
