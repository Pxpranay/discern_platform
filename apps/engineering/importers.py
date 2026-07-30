"""Import a BOQ revision from the spreadsheet format Discern actually uses.

Their files carry two banner rows (project title, then "Rev-N, Date-..."), a
header row, and then SL NO / DESCRIPTION / UNIT / QTY. There is no rate column
and no item-master reference, so an imported line is free text with a quantity
— which is exactly why ``BoqLine.item`` is nullable.
"""

import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.core.models import BoqLine

from .models import BoqRevision, BoqSection, Discipline

HEADER_TOKENS = {"sl no", "sl. no", "slno", "description", "unit", "qty", "quantity"}


def _as_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def parse_workbook(path) -> dict:
    """Read the sheet into ``{"revision_label", "rows": [...]}``."""
    import openpyxl

    ws = openpyxl.load_workbook(path, data_only=True).active

    revision_label = ""
    rows: list[dict] = []
    header_seen = False

    for raw in ws.iter_rows(values_only=True):
        cells = ["" if c is None else str(c).strip() for c in raw]
        if not any(cells):
            continue

        joined = " ".join(cells).lower()
        if not header_seen:
            if re.search(r"\brev[-\s]?\d+", joined):
                revision_label = cells[0]
            if any(tok in joined for tok in ("description",)) and "unit" in joined:
                header_seen = True
            continue

        sl_no, description, uom, qty = (cells + ["", "", "", ""])[:4]
        if not description or description.lower() in HEADER_TOKENS:
            continue
        quantity = _as_decimal(qty)
        if quantity is None:
            continue
        rows.append(
            {"sl_no": sl_no, "description": description, "uom": uom, "quantity": quantity}
        )

    return {"revision_label": revision_label, "rows": rows}


@transaction.atomic
def import_revision(
    *,
    path,
    project,
    revision_number: int,
    discipline: str = Discipline.GOODS,
    route: str = BoqLine.SUPPLY,
    lot=None,
    signed_off_by=None,
) -> BoqRevision:
    """Create a revision from a spreadsheet.

    Imported revisions have no ``previous_line`` set by construction, so the
    diff falls back to normalized description matching — the path this importer
    exists to exercise.

    ``signed_off_by`` records who approved the document *outside* the system.
    An imported BOQ is normally a historical one that was prepared and signed
    on paper, so leaving it permanently unsignable would block the migration
    path in build plan §9, where in-flight projects enter as an opening
    revision. Omit it and the revision behaves like any other draft, needing
    sign-off in-app before it can be released.
    """
    parsed = parse_workbook(path)

    revision = BoqRevision.objects.create(
        project=project, revision_number=revision_number
    )
    sections = {
        d: BoqSection.objects.create(revision=revision, discipline=d)
        for d in (Discipline.GOODS, Discipline.SERVICE)
    }
    target = sections[discipline]

    for row in parsed["rows"]:
        BoqLine.objects.create(
            project=project,
            section=target,
            lot=lot,
            sl_no=row["sl_no"],
            description=row["description"],
            quantity=row["quantity"],
            uom=row["uom"],
            route=route,
        )

    other = Discipline.SERVICE if discipline == Discipline.GOODS else Discipline.GOODS
    sections[other].is_not_applicable = True
    sections[other].signed_off_at = timezone.now()
    sections[other].save(update_fields=["is_not_applicable", "signed_off_at"])

    if signed_off_by is not None:
        target.owner = signed_off_by
        target.signed_off_by = signed_off_by
        target.signed_off_at = timezone.now()
        target.save(update_fields=["owner", "signed_off_by", "signed_off_at"])
        revision.status = BoqRevision.SECTIONS_SIGNED
        revision.save(update_fields=["status"])

    return revision
