"""Posting to and querying the stock ledger.

On-hand is the sum of moves. Cross-location availability — the earlier design's
"genuinely custom joined report" — is an ordinary query here, because the
schema is ours (process design §3.8).
"""

from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from ..models import StockMove


def post_move(
    *,
    item_id: int,
    quantity: Decimal,
    source_type: str,
    source_id: int,
    actor,
    from_location_id: int | None = None,
    to_location_id: int | None = None,
    unit_value: Decimal | None = None,
    effective_at=None,
    boq_line_id: int | None = None,
) -> StockMove:
    return StockMove.objects.create(
        item_id=item_id,
        boq_line_id=boq_line_id,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
        quantity=Decimal(quantity),
        unit_value=unit_value,
        source_type=source_type,
        source_id=source_id,
        effective_at=effective_at or timezone.now(),
        actor=actor,
    )


def on_hand(item_id: int, location_id: int) -> Decimal:
    inbound = StockMove.objects.filter(item_id=item_id, to_location_id=location_id).aggregate(
        t=Sum("quantity")
    )["t"] or Decimal("0")
    outbound = StockMove.objects.filter(
        item_id=item_id, from_location_id=location_id
    ).aggregate(t=Sum("quantity"))["t"] or Decimal("0")
    return inbound - outbound


def availability(item_id: int) -> list[dict]:
    """On-hand for one item across every location that has ever held it.

    This is the stock side of the availability statement the Purchase Manager
    sees before any RFQ goes out (process design §4.7).
    """
    inbound = {
        r["to_location_id"]: r["t"]
        for r in StockMove.objects.filter(item_id=item_id, to_location__isnull=False)
        .values("to_location_id")
        .annotate(t=Sum("quantity"))
    }
    outbound = {
        r["from_location_id"]: r["t"]
        for r in StockMove.objects.filter(item_id=item_id, from_location__isnull=False)
        .values("from_location_id")
        .annotate(t=Sum("quantity"))
    }
    rows = []
    for location_id in sorted(set(inbound) | set(outbound)):
        qty = (inbound.get(location_id) or Decimal("0")) - (
            outbound.get(location_id) or Decimal("0")
        )
        rows.append({"location_id": location_id, "on_hand": qty})
    return rows


def valuation_at(item_id: int, location_id: int) -> Decimal | None:
    """Unit value of stock held at a location — its original purchase cost.

    Decision #4: original cost, not replacement cost. It is factual, needs no
    judgement, and keeps both sides of an inter-project transfer reconcilable
    to actual spend. Replacement cost would invent a gain or loss that neither
    project caused.

    Taken from the most recent inbound move carrying a value, which for
    material bought in is the rate on the purchase order line it arrived on.
    """
    move = (
        StockMove.objects.filter(
            item_id=item_id, to_location_id=location_id, unit_value__isnull=False
        )
        .order_by("-effective_at", "-id")
        .first()
    )
    return move.unit_value if move else None
