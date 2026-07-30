"""Domain exceptions.

These are business outcomes, not bugs. Callers are expected to catch them and
turn them into a clear message naming what blocked and why — the design is
explicit that a blocked document says so rather than failing quietly
(process design §5.1).
"""

from decimal import Decimal


class DomainError(Exception):
    """Base for every business-rule violation raised by the platform."""


class CeilingExceeded(DomainError):
    """A document tried to authorize more than the BOQ line permits.

    Carries the numbers so the caller can tell the user exactly how much
    headroom was available, rather than only that it failed.
    """

    def __init__(self, boq_line_id: int, requested: Decimal, available: Decimal):
        self.boq_line_id = boq_line_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"BOQ line {boq_line_id}: requested {requested} but only {available} "
            f"remains authorized. Raise the quantity in a new BOQ revision to proceed."
        )


class OverRelease(DomainError):
    """A document tried to release more commitment than it ever consumed.

    Guards the arithmetic that makes returns net out correctly: without it, a
    duplicated return would manufacture headroom that no revision authorized.
    """

    def __init__(self, document_type: str, document_id: int, requested, held):
        self.document_type = document_type
        self.document_id = document_id
        super().__init__(
            f"{document_type} {document_id} holds {held} against this BOQ line "
            f"but tried to release {requested}."
        )


class RecordLocked(DomainError):
    """An approved record was written without an Administrator override.

    Process design §5.2: free to edit until approved, locked the moment
    approval fires.
    """

    def __init__(self, obj):
        self.obj = obj
        super().__init__(
            f"{obj.__class__.__name__} {obj.pk} was locked on approval. "
            f"Change it through a new revision or amendment, or an "
            f"Administrator override."
        )


class NotInTransaction(DomainError):
    """A ledger write or event emission was attempted outside a transaction.

    The ceiling's row lock and the outbox's atomicity guarantee both depend on
    the caller's transaction. Silently working without one would mean the
    guarantee is not actually held, which is worse than failing here.
    """


class MissingReason(DomainError):
    """An Administrator override was attempted without a reason."""
