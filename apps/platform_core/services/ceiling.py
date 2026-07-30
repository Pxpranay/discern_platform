"""The BOQ quantity ceiling.

Process design §3.6 and §5.1. Every document that authorizes quantity against a
BOQ line — purchase order, service order, fabrication order, site requisition —
calls ``reserve_headroom``. There is exactly one code path, so a fifth document
type added in three years gets the ceiling for free by calling the same
function, and cannot acquire it by retrofit the way the earlier design's three
separate validations did.

Two properties this module exists to guarantee:

1. **Returns net out.** Releasing headroom is consumption with the opposite
   sign, so a return, cancellation or amendment restores exactly what it
   should. The earlier design summed non-cancelled order lines, which never
   released headroom on a return — the quantity stayed committed forever and
   re-ordering that material was impossible.

2. **Concurrent callers serialize.** The BOQ line row is locked with
   ``SELECT FOR UPDATE`` before the sum is read, so two buyers confirming at
   the same moment cannot both see the same headroom and both pass.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.core.models import BoqLine

from ..exceptions import CeilingExceeded, NotInTransaction, OverRelease
from ..models import CommitmentEntry

ZERO = Decimal("0")


def _require_transaction() -> None:
    """The row lock is only meaningful inside the caller's transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise NotInTransaction(
            "reserve_headroom/release_headroom must run inside transaction.atomic(); "
            "the ceiling's row lock depends on the caller's transaction."
        )


def tolerance_for(boq_line: BoqLine) -> Decimal:
    """Absolute quantity allowed above the BOQ figure.

    Decision #1: configured per item category, defaulting to zero, so a
    consumable like cement can carry a wastage allowance while a discrete item
    like a pump carries none. With every category left at the default this
    returns zero and the ceiling is exactly the BOQ quantity.
    """
    category = getattr(boq_line.item, "category", None)
    pct = getattr(category, "wastage_tolerance_pct", None) or ZERO
    if pct == ZERO:
        return ZERO
    return (boq_line.quantity * pct) / Decimal("100")


def committed_qty(boq_line_id: int) -> Decimal:
    """Net quantity authorized so far. The sum *is* the total — never stored."""
    total = CommitmentEntry.objects.filter(boq_line_id=boq_line_id).aggregate(
        total=Sum("qty_delta")
    )["total"]
    return total if total is not None else ZERO


def document_holding(boq_line_id: int, document_type: str, document_id: int) -> Decimal:
    """Net quantity this specific document currently holds against the line."""
    total = CommitmentEntry.objects.filter(
        boq_line_id=boq_line_id,
        document_type=document_type,
        document_id=document_id,
    ).aggregate(total=Sum("qty_delta"))["total"]
    return total if total is not None else ZERO


def ceiling_for(boq_line: BoqLine) -> Decimal:
    return boq_line.quantity + tolerance_for(boq_line)


def headroom(boq_line_id: int) -> Decimal:
    """Quantity still authorizable against this line, right now."""
    line = BoqLine.objects.select_related("item__category").get(pk=boq_line_id)
    return ceiling_for(line) - committed_qty(boq_line_id)


def reserve_headroom(
    *,
    boq_line_id: int,
    qty: Decimal,
    document_type: str,
    document_id: int,
    actor,
    reason: str,
    override_actor=None,
    override_reason: str = "",
) -> CommitmentEntry:
    """Consume ceiling headroom, or raise :class:`CeilingExceeded`.

    Deliberately **not** decorated ``@transaction.atomic``. The caller must open
    the transaction, for two reasons:

    * The commitment and the document that caused it must commit together. If
      this function opened its own transaction, a caller who forgot theirs
      would leave a commitment recorded against a document that never
      committed.
    * The row lock must be held until the caller commits, not until this
      function returns. An inner transaction would release it early and reopen
      the race it exists to close.

    ``_require_transaction`` enforces that rather than trusting it.

    ``override_actor``/``override_reason`` implement decision #2 — the Project
    Manager's logged emergency override. An override still writes a normal
    commitment entry, so the ledger arithmetic is unaffected; what changes is
    that the entry is permitted past the ceiling and is marked for
    reconciliation into the next BOQ revision.
    """
    if qty is None or Decimal(qty) <= ZERO:
        raise ValueError("reserve_headroom requires a positive quantity.")
    qty = Decimal(qty)
    _require_transaction()

    # Serializes concurrent callers on this line before the sum is read.
    # ``of=("self",)`` locks only the boq_line row: the category join is an
    # outer join (category is nullable) and Postgres refuses FOR UPDATE on the
    # nullable side of one. Locking the line is all that is wanted anyway —
    # the item and category are reference data, not contended state.
    line = (
        BoqLine.objects.select_for_update(of=("self",))
        .select_related("item__category")
        .get(pk=boq_line_id)
    )
    available = ceiling_for(line) - committed_qty(boq_line_id)

    if qty > available:
        if override_actor is None:
            raise CeilingExceeded(boq_line_id, requested=qty, available=available)
        if not override_reason.strip():
            raise ValueError("A ceiling override requires a reason.")
        reason = f"[CEILING OVERRIDE by {override_actor}] {override_reason} :: {reason}"
        actor = override_actor

    return CommitmentEntry.objects.create(
        boq_line_id=boq_line_id,
        project_id=line.project_id,
        document_type=document_type,
        document_id=document_id,
        qty_delta=qty,
        reason=reason,
        actor=actor,
    )


def release_headroom(
    *,
    boq_line_id: int,
    qty: Decimal,
    document_type: str,
    document_id: int,
    actor,
    reason: str,
) -> CommitmentEntry:
    """Return headroom a document no longer holds.

    Used by cancellation, amendment-down, and material return. A document may
    never release more than it holds — without that guard a duplicated return
    would manufacture headroom no BOQ revision ever authorized.

    Caller supplies the transaction, for the same reasons as
    :func:`reserve_headroom`.
    """
    if qty is None or Decimal(qty) <= ZERO:
        raise ValueError("release_headroom requires a positive quantity.")
    qty = Decimal(qty)
    _require_transaction()

    line = BoqLine.objects.select_for_update().get(pk=boq_line_id)
    held = document_holding(boq_line_id, document_type, document_id)
    if qty > held:
        raise OverRelease(document_type, document_id, requested=qty, held=held)

    return CommitmentEntry.objects.create(
        boq_line_id=boq_line_id,
        project_id=line.project_id,
        document_type=document_type,
        document_id=document_id,
        qty_delta=-qty,
        reason=reason,
        actor=actor,
    )
