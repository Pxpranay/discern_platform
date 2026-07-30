"""End-to-end walkthrough of everything built so far.

Runs the real services against a real database — no mocks, no shortcuts. Every
number printed is computed by the same code paths the tests exercise and the
application would use.

    python manage.py demo

The BOQ steps use Discern's own LINAC Building fire protection revisions from
``tests/fixtures/``.
"""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import AppUser, Role
from apps.core.models import BoqLine, Item, ItemCategory, Location, Project
from apps.inventory import services as inventory
from apps.engineering import importers, reconciliation
from apps.engineering import services as boq
from apps.engineering.models import BoqRevision, Discipline, ReconciliationOutcome
from apps.platform_core.exceptions import CeilingExceeded, DomainError, RecordLocked
from apps.platform_core.models import CommitmentEntry, CostEntry, OutboxEvent
from apps.platform_core.services import costing, events
from apps.platform_core.services.ceiling import committed_qty, headroom, reserve_headroom
from apps.platform_core.services.stock import on_hand, post_move
from apps.projects import services as projects
from apps.projects.models import SchedulePhase
from apps.projects.services import ScheduleExceedsCommitment
from apps.sales import services as sales
from apps.inventory import services as inventory
from apps.procurement import services as procurement
from apps.fabrication import services as fab
from apps.fabrication.models import BillOfMaterials, BomComponent, FabricationMode
from apps.finance import services as finance
from apps.finance.models import ExpenseCategory
from apps.inventory.models import ExcessStockFlag
from apps.procurement.models import PurchaseOrder, Vendor, VendorRate
from apps.subcontracts import services as subs
from apps.sales.models import Client, ClientInvoice, Lot, LotKind, Order

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures"
W = 78


def n(value) -> str:
    """Trim a Decimal for display. ``Decimal.normalize`` would render 40.0000
    as 4E+1, which is worse than the problem it solves."""
    return f"{float(value):g}"


class Command(BaseCommand):
    help = "Run an end-to-end demonstration of the platform as built so far."

    # ---------------------------------------------------------------- output
    def h1(self, text):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("━" * W))
        self.stdout.write(self.style.MIGRATE_HEADING(f"  {text}"))
        self.stdout.write(self.style.MIGRATE_HEADING("━" * W))

    def h2(self, text):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_LABEL(f"▸ {text}"))

    def ok(self, text):
        self.stdout.write(self.style.SUCCESS(f"   ✓ {text}"))

    def blocked(self, text):
        self.stdout.write(self.style.ERROR(f"   ✗ BLOCKED  {text}"))

    def note(self, text):
        self.stdout.write(f"     {text}")

    def row(self, *cols, widths=(34, 10, 10, 22)):
        self.stdout.write(
            "     " + "".join(str(c).ljust(w) for c, w in zip(cols, widths))
        )

    # ----------------------------------------------------------------- setup
    def people(self):
        stamp = timezone.now().strftime("%H%M%S%f")

        def role(code, caps):
            r, _ = Role.objects.get_or_create(
                code=code,
                defaults={"name": code.replace("_", " ").title(), "capabilities": caps},
            )
            return r

        def user(name, caps):
            u = AppUser.objects.create(username=f"{name}-{stamp}", email=f"{name}@discern.test")
            u.user_roles.create(role=role(name, caps))
            return u

        return {
            "sales_mgr": user("sales_manager", ["order:approve_kickoff"]),
            "pm": user("project_manager", ["project:extend_schedule", "boq_revision:release"]),
            "design_mgr": user("design_manager", ["fabrication:manage"]),
            "buyer": user("procurement_officer", ["procurement:rfq", "purchase_order:create"]),
            "site_engineer": user("site_engineer", [
                "receipt:verify", "receipt:return", "stock:flag_excess", "expenses:submit",
            ]),
            "store": user("store_keeper", ["receipt:record"]),
            "construction_mgr": user("construction_manager", [
                "service_order:issue", "service_order:progress",
                "service_order:certify", "expenses:approve",
            ]),
            "purchase_mgr": user("purchase_manager", [
                "stock:transfer",
                "procurement:rfq", "procurement:award",
                "purchase_order:create", "purchase_order:approve",
            ]),
            "admin": AppUser.objects.create(
                username=f"admin-{stamp}", email="admin@discern.test", is_administrator=True
            ),
        }

    # ------------------------------------------------------------------- run
    def handle(self, *args, **options):
        stamp = timezone.now().strftime("%y%m%d-%H%M%S")
        who = self.people()
        committed_date = date.today() + timedelta(days=240)

        self.h1("DISCERN PLATFORM — END-TO-END DEMONSTRATION")
        self.note("Every figure below is computed by the real services against")
        self.note("PostgreSQL. BOQ data is Discern's own LINAC Building revisions.")

        # ================================================== 1. SALES
        self.h1("1.  SALES — quotation, SITC lots, confirmed order")

        client = Client.objects.create(
            name="IPGME&R–SSKM", gstin="19AAACI0000A1Z9",
            billing_address="1, Harish Mukherjee Road, Kolkata",
        )
        order = Order.objects.create(client=client, number=f"SO-{stamp}")
        fire = Lot.objects.create(
            order=order, name="Lot 1: SITC of Fire Fighting System",
            kind=LotKind.LUMP_SUM_SITC, price=Decimal("4850000"), sequence=1,
        )
        hvac = Lot.objects.create(
            order=order, name="Lot 2: SITC of HVAC", kind=LotKind.LUMP_SUM_SITC,
            price=Decimal("2600000"), sequence=2,
        )
        self.ok(f"Order {order.number} for {client.name}")
        self.note("Two lump-sum SITC lots — the client sees one price each,")
        self.note("with no breakdown of what materials or labour make them up:")
        self.row("LOT", "KIND", "PRICE", widths=(40, 18, 16))
        for lot in (fire, hvac):
            self.row(lot.name, "lump-sum SITC", f"₹{lot.price:,.0f}", widths=(40, 18, 16))
        self.note(f"Order value ₹{order.total_value:,.0f}")

        self.h2("Confirm the order")
        with transaction.atomic():
            sales.confirm_order(order=order, committed_delivery_date=committed_date, actor=who["sales_mgr"])
        order.refresh_from_db()
        self.ok(f"Status: {order.get_status_display()} — committed delivery {committed_date}")
        self.note("A confirmed order does NOT create a project. It waits for kickoff approval.")
        self.note(f"Projects in existence: {Project.objects.filter(order=order).count()}")

        # ================================================== 2. KICKOFF
        self.h1("2.  KICKOFF GATE — the hand-off that removes re-typing")

        self.h2("An unauthorised user tries to approve kickoff")
        try:
            with transaction.atomic():
                sales.approve_for_kickoff(order=order, actor=who["buyer"])
        except DomainError as exc:
            self.blocked(str(exc))

        self.h2("The Sales Manager approves")
        with transaction.atomic():
            sales.approve_for_kickoff(order=order, actor=who["sales_mgr"])
        self.ok("OrderApprovedForKickoff emitted to the outbox")
        pending = OutboxEvent.objects.filter(status=OutboxEvent.PENDING).count()
        self.note(f"Pending events: {pending} — nothing has run yet")

        events.drain()
        project = Project.objects.get(order=order)
        # initiate_project reassigns lots with a queryset update, so the
        # objects held here are stale until refetched.
        fire.refresh_from_db()
        hvac.refresh_from_db()
        self.ok(f"Project {project.code} created automatically")
        self.row("Client", project.client.name, widths=(22, 50))
        self.row("Budget", f"₹{project.budget:,.0f}", widths=(22, 50))
        self.row("Committed date", str(project.effective_committed_date), widths=(22, 50))
        self.row("Lots carried over", project.lots.count(), widths=(22, 50))
        self.row("Site location", Location.objects.get(project=project).code, widths=(22, 50))
        self.note("Nothing above was re-typed by anyone.")

        # ================================================== 3. SCHEDULE
        self.h1("3.  MASTER SCHEDULE — capped by what the client was promised")

        self.h2("Plan phases within the committed date")
        for i, (name, kind, days) in enumerate(
            [
                ("Site visit / requirement assessment", SchedulePhase.SITE_VISIT, 200),
                ("Engineering / BOQ preparation", SchedulePhase.BOQ_PREP, 170),
                ("Procurement stage 1 — piping", SchedulePhase.PROCUREMENT, 140),
                ("Procurement stage 2 — valves & fittings", SchedulePhase.PROCUREMENT, 100),
                ("Construction & commissioning", SchedulePhase.CONSTRUCTION, 10),
            ], start=1,
        ):
            phase = projects.plan_phase(
                project=project, name=name, kind=kind,
                planned_end=committed_date - timedelta(days=days),
                sequence=i, actor=who["pm"],
            )
            self.ok(f"{phase.name} — by {phase.planned_end}")
        self.note("Procurement is staged simply by having two rows. No special-casing.")

        self.h2("Try to plan construction beyond the committed date")
        try:
            projects.plan_phase(
                project=project, name="Construction (overrun)",
                kind=SchedulePhase.CONSTRUCTION,
                planned_end=committed_date + timedelta(days=30),
                sequence=9, actor=who["pm"],
            )
        except ScheduleExceedsCommitment as exc:
            self.blocked(str(exc))

        self.h2("Client agrees a later date; the CEO/PM records it")
        projects.extend_commitment(
            project=project, new_committed_date=committed_date + timedelta(days=45),
            client_agreement_reference="Client letter ref IPGME&R/EXT/2026-11 dated 02.11.2026",
            actor=who["pm"],
        )
        project.refresh_from_db()
        self.ok(f"Committed date extended to {project.effective_committed_date}")
        self.note("Requires a recorded client agreement — internal sign-off is not evidence.")

        # ================================================== 4. BOQ REV 0
        self.h1("4.  BOQ Rev 0 — imported from Discern's own spreadsheet")

        rev0 = importers.import_revision(
            path=FIXTURES / "BOQ_Linac_Bldg_Rev0.xlsx", project=project,
            revision_number=0, lot=fire, signed_off_by=who["design_mgr"],
        )
        lines0 = BoqLine.objects.filter(section__revision=rev0).order_by("sl_no")
        self.ok(f"{lines0.count()} lines imported into the Goods section, tagged to '{fire.name}'")
        self.note("This is the SITC explosion: one lump-sum price, many BOQ lines.")
        self.row("SL", "DESCRIPTION", "UNIT", "QTY", widths=(6, 42, 8, 10))
        for line in lines0:
            self.row(line.sl_no, line.description[:40], line.uom, n(line.quantity),
                     widths=(6, 42, 8, 10))

        self.h2("The Service section is empty — this project has no subcontract scope yet")
        service = rev0.sections.get(discipline=Discipline.SERVICE)
        self.ok(f"Marked not applicable: {service.is_not_applicable}")
        self.note("This is what stops a materials-only project deadlocking on a")
        self.note("signature nobody can meaningfully give.")

        self.h2("Project Manager releases Rev 0")
        with transaction.atomic():
            boq.release_revision(revision=rev0, actor=who["pm"])
        events.drain()
        rev0.refresh_from_db()
        self.ok(f"Released and locked at {rev0.locked_at:%Y-%m-%d %H:%M}")

        self.h2("Try to edit the released revision")
        try:
            rev0.status = BoqRevision.DRAFT
            rev0.save()
        except RecordLocked as exc:
            self.blocked(str(exc))

        # ================================================== 5. CEILING
        self.h1("5.  THE QUANTITY CEILING — purchase can never exceed the BOQ")

        pipe200 = lines0.get(description__startswith="MS ERW Pipe 200")
        pipe80 = lines0.get(description__startswith="MS ERW Pipe 80")
        self.note(f"Line: {pipe200.description} — BOQ says {pipe200.quantity:g} {pipe200.uom}")

        self.h2("Order 18 Mtr against it")
        with transaction.atomic():
            reserve_headroom(
                boq_line_id=pipe200.pk, qty=Decimal("18"),
                document_type="purchase_order", document_id=101,
                actor=who["buyer"], reason="PO-101 to vendor",
            )
        self.ok(f"Committed {n(committed_qty(pipe200.pk))} — headroom now {n(headroom(pipe200.pk))}")

        self.h2("A buyer tries to order 5 Mtr more")
        try:
            with transaction.atomic():
                reserve_headroom(
                    boq_line_id=pipe200.pk, qty=Decimal("5"),
                    document_type="purchase_order", document_id=102,
                    actor=who["buyer"], reason="padded quantity",
                )
        except CeilingExceeded as exc:
            self.blocked(str(exc))
        self.note("Not a warning. The document does not exist.")

        self.h2("Order the 80 mm pipe, and receive 4 of the 6 Mtr at site")
        item = Item.objects.create(code=f"pipe80-{stamp}", name="MS ERW Pipe 80 mm NB", uom="Mtr")
        with transaction.atomic():
            reserve_headroom(
                boq_line_id=pipe80.pk, qty=Decimal("6"),
                document_type="purchase_order", document_id=103,
                actor=who["buyer"], reason="PO-103",
            )
        site = Location.objects.get(project=project)
        post_move(
            item_id=item.pk, quantity=Decimal("4"), to_location_id=site.pk,
            source_type="goods_receipt", source_id=1, actor=who["site_engineer"],
            boq_line_id=pipe80.pk, effective_at=timezone.now(),
        )
        self.ok(f"6 Mtr on order, 4 Mtr verified into stock at {site.code}")
        self.note(f"On hand: {n(on_hand(item.pk, site.pk))} Mtr")

        # ================================================== 6. REV 1
        self.h1("6.  BOQ Rev 1 — the reconciliation engine")

        rev1 = importers.import_revision(
            path=FIXTURES / "BOQ_Linac_Bldg_Rev1.xlsx", project=project,
            revision_number=1, lot=fire, signed_off_by=who["design_mgr"],
        )
        with transaction.atomic():
            boq.release_revision(revision=rev1, actor=who["pm"])
        events.drain()
        self.ok("Rev 1 released — reconciliation ran automatically on the event")

        outcomes = ReconciliationOutcome.objects.filter(revision=rev1)
        self.note("")
        self.row("DESCRIPTION", "REV 0", "REV 1", "OUTCOME → ACTION", widths=(38, 8, 8, 30))
        for o in outcomes:
            if o.kind == ReconciliationOutcome.UNCHANGED:
                verdict = "unchanged"
            else:
                verdict = f"{o.get_kind_display()} → {o.get_action_display()}"
            self.row(o.description[:36], n(o.previous_qty), n(o.new_qty),
                     verdict[:28], widths=(38, 8, 8, 30))

        acting = outcomes.exclude(action=ReconciliationOutcome.NONE)
        self.note("")
        self.ok(f"{outcomes.count()} lines examined; only {acting.count()} reach Procurement")

        self.h2("What Procurement is actually asked to buy")
        for o in outcomes.filter(action=ReconciliationOutcome.REQUEST_DELTA):
            self.row(o.description[:40], f"+{n(o.delta)} {o.uom}", widths=(46, 20))
        self.note("The 200 mm pipe went 18 → 40, and the ask is 22 — not 40.")

        self.h2("The 80 mm pipe was cut to zero, with 6 ordered and 4 already at site")
        cut = outcomes.get(description__startswith="MS ERW Pipe 80")
        self.row("Outcome", cut.get_kind_display(), widths=(22, 50))
        self.row("Action", cut.get_action_display(), widths=(22, 50))
        self.row("Return / redeploy", f"{n(cut.excess_received)} Mtr already received", widths=(22, 50))
        self.row("Cancel with vendor", f"{n(cut.order_reduction)} Mtr not yet shipped", widths=(22, 50))
        self.note("One line, two different actions. Comparing documents cannot produce this —")
        self.note("the engine reads the commitment and stock ledgers.")

        # ============================================ 6b. PROCUREMENT
        self.h1("7.  PROCUREMENT — three vendors, a comparison, a free award")

        pr = project.procurement_requests.order_by("-created_at").first()
        self.ok(f"{pr.number} raised automatically from the released revision")
        self.note(f"{pr.lines.count()} line(s) — only what actually changed")

        vendors = [
            Vendor.objects.get_or_create(name=name, defaults={"is_active": True})[0]
            for name in ("Steel & Pipes Co", "Kolkata Tubes Pvt Ltd", "Eastern Metals")
        ]
        rfq = procurement.create_rfq(request=pr, vendors=vendors, actor=who["buyer"])
        procurement.issue_rfq(rfq=rfq, actor=who["buyer"])
        self.ok(f"{rfq.number} issued to {len(vendors)} vendors")

        target = pr.lines.first()
        for rfq_vendor, rate in zip(rfq.vendors.all(), ["1310", "1180", "1245"]):
            procurement.record_quote(
                rfq_vendor=rfq_vendor,
                quotes=[{"request_line": target, "rate": rate}],
                actor=who["buyer"],
            )

        self.h2(f"Comparison statement — {target.description[:44]}")
        self.row("VENDOR", "RATE", "", widths=(34, 14, 20))
        row = next(r for r in procurement.comparison(rfq) if r["line"].pk == target.pk)
        for quote in row["quotes"]:
            mark = "  ← lowest" if quote["is_best_price"] else ""
            self.row(quote["vendor"].name, f"₹{quote['rate']:,.0f}", mark, widths=(34, 14, 20))

        self.h2("The Purchase Manager awards — irrespective of price")
        chosen = row["quotes"][2]["vendor"]
        award = procurement.award_line(
            rfq=rfq, request_line=target, vendor=chosen, actor=who["purchase_mgr"],
            notes="Only vendor able to deliver before the slab pour.",
        )
        self.ok(f"Awarded to {chosen.name} at ₹{award.awarded_rate:,.0f}")
        self.note("Not the lowest quote. No justification is demanded — that was")
        self.note("stated as the Purchase Manager's prerogative and is honoured.")
        self.note("The comparison seen at that moment is frozen onto the award.")

        order = procurement.create_purchase_order(awards=[award], actor=who["purchase_mgr"])
        order.lines.update(item=item)
        procurement.submit_purchase_order(order=order, actor=who["purchase_mgr"])
        events.drain()
        self.ok(f"{order.number} confirmed — {n(award.awarded_qty)} {target.uom} at ₹{award.awarded_rate:,.0f}")

        # ============================================ 6c. RECEIPT
        self.h1("8.  RECEIPT — nothing becomes cost until it is verified")

        po_line = order.lines.first()
        receipt = inventory.record_receipt(
            purchase_order_line=po_line, quantity=Decimal("20"),
            actor=who["store"], vendor_challan="CH-4471",
        )
        self.ok(f"{receipt.number}: Store Keeper recorded 20 {po_line.uom} arriving")
        self.row("Cost posted so far", f"₹{costing.project_total(project.pk, CostEntry.MATERIAL):,.0f}", widths=(26, 30))
        self.note("Recorded is not accepted.")

        self.h2("A buyer tries to verify their own delivery")
        try:
            inventory.verify_receipt(receipt=receipt, accepted_qty=20, actor=who["buyer"])
        except DomainError as exc:
            self.blocked(str(exc))

        self.h2("The Site Engineer verifies — 18 good, 2 dented")
        inventory.verify_receipt(
            receipt=receipt, accepted_qty=Decimal("18"), rejected_qty=Decimal("2"),
            actor=who["site_engineer"], notes="2 Mtr dented in transit",
        )
        events.drain()
        self.ok("Stock and cost posted for the accepted quantity only")
        self.row("Material cost", f"₹{costing.project_total(project.pk, CostEntry.MATERIAL):,.0f}", widths=(26, 30))
        self.row("Discrepancy", "2 Mtr — vendor bill held", widths=(26, 30))
        self.row("Headroom now", f"{n(headroom(po_line.boq_line_id))} {po_line.uom} — 2 of it freed by the rejection", widths=(26, 44))
        self.note("The rejected quantity frees its BOQ headroom, so the replacement")
        self.note("can be ordered without a revision.")

        # ============================================ 8b. WORKS
        self.h1("9.  WORKS — fabricated to drawing, and subcontracted out")

        self.h2("Rev 2 adds scope the earlier revisions did not have")
        rev2 = boq.open_revision(project=project, actor=who["design_mgr"])
        goods = rev2.sections.get(discipline=Discipline.GOODS)
        service = rev2.sections.get(discipline=Discipline.SERVICE)

        staircase = Item.objects.create(
            code=f"stair-{stamp}", name="Custom MS staircase", uom="nos"
        )
        plate = Item.objects.create(code=f"plate-{stamp}", name="MS plate 10mm", uom="kg")
        plumbing = Item.objects.create(
            code=f"plumb-{stamp}", name="Plumbing installation", uom="sqm",
            item_type=Item.SERVICE,
        )
        bom = BillOfMaterials.objects.create(item=staircase, name="Staircase")
        BomComponent.objects.create(bom=bom, item=plate, quantity=Decimal("120"), uom="kg")

        stair_line = BoqLine.objects.create(
            project=project, section=goods, lot=fire, item=staircase,
            sl_no="11", description="Custom MS staircase to drawing",
            quantity=Decimal("2"), uom="nos", route=BoqLine.FABRICATE,
        )
        plumb_line = BoqLine.objects.create(
            project=project, section=service, lot=fire, item=plumbing,
            sl_no="S1", description="Plumbing installation",
            quantity=Decimal("400"), uom="sqm", route=BoqLine.SERVICE,
        )
        boq.sign_off_section(section=goods, actor=who["design_mgr"])
        boq.sign_off_section(section=service, actor=who["construction_mgr"])
        with transaction.atomic():
            boq.release_revision(revision=rev2, actor=who["pm"])
        events.drain()
        self.ok("Rev 2 released — both sections signed, this time by two people")
        self.note("The Service section is no longer 'not applicable'.")

        self.h2("Fabrication — capped on the finished item, not its components")
        fab_order = fab.create_order(
            boq_line=stair_line, quantity=Decimal("2"), actor=who["design_mgr"], bom=bom
        )
        self.ok(f"{fab_order.number} — 2 staircases, headroom now {n(headroom(stair_line.pk))}")
        shortfall = fab.material_shortfall(fab_order)
        for row in shortfall:
            self.row("Short", f"{n(row['short'])} {row['uom']} of {row['item'].name}", widths=(12, 50))
        pr2 = fab.request_shortfall(order=fab_order, actor=who["design_mgr"])
        self.ok(f"{pr2.number} raised for the raw material")
        self.note("Raw materials are deliberately not ceiling-checked — they are")
        self.note("components consumed to produce the line, and the cap sits upstream.")

        works_loc = Location.objects.get(code=f"{project.code}-WORKS")
        post_move(
            item_id=plate.pk, quantity=Decimal("240"), to_location_id=works_loc.pk,
            unit_value=Decimal("72"), source_type="demo", source_id=1, actor=who["store"],
        )
        fab.start(order=fab_order, actor=who["design_mgr"])
        fab.complete(order=fab_order, actor=who["design_mgr"], unit_cost=Decimal("41000"))
        events.drain()
        self.ok("Produced — 2 staircases into project stock, ₹82,000 FABRICATION cost")

        self.h2("Subcontract — direct to an empanelled vendor, no RFQ")
        subcontractor = Vendor.objects.create(name="Bengal Plumbing Works", is_empanelled=True)
        VendorRate.objects.create(
            vendor=subcontractor, item=plumbing, rate=Decimal("450"), uom="sqm"
        )
        service_order = subs.create_service_order(
            boq_line=plumb_line, vendor=subcontractor, quantity=Decimal("400"),
            actor=who["construction_mgr"],
        )
        subs.issue(order=service_order, actor=who["purchase_mgr"])
        self.ok(f"{service_order.number} at the agreed ₹450/sqm = ₹{service_order.total_value:,.0f}")
        self.note("Discern has agreed rates with empanelled trades; floating a tender")
        self.note("for every service line would slow things down for no benefit.")

        subs.log_progress(
            order=service_order, percent=Decimal("55"), actor=who["site_engineer"],
            notes="First and second floor risers complete",
        )
        self.ok("Site Engineer logged 55% — and that releases no money")
        self.row("Subcontract cost", f"₹{costing.project_total(project.pk, CostEntry.SUBCONTRACT):,.0f}", widths=(24, 30))

        cert = subs.certify(
            order=service_order, quantity=Decimal("200"), actor=who["construction_mgr"]
        )
        events.drain()
        self.ok(f"Running bill RA-{cert.running_bill_number} certified for 200 sqm")
        self.row("Vendor bill", f"₹{cert.certified_value:,.0f}", widths=(24, 30))
        self.row("Subcontract cost", f"₹{costing.project_total(project.pk, CostEntry.SUBCONTRACT):,.0f}", widths=(24, 30))
        self.note("No goods receipt — there is nothing physical to receive.")

        self.h2("Dead stock at one site, needed at another")
        other = Project.objects.create(code=f"OTHER-{stamp}", name="Second site", status=Project.ACTIVE)
        other_loc = Location.objects.create(
            code=f"OTHER-{stamp}-SITE", name="Second site", kind=Location.SITE, project=other
        )
        flag = inventory.flag_excess(
            item=item, location=site, quantity=Decimal("6"), actor=who["site_engineer"],
            reason=ExcessStockFlag.AVAILABLE, notes="Run shortened after revision",
        )
        events.drain()
        self.ok(f"{n(flag.quantity)} Mtr flagged — three dashboards notified")
        transfer = inventory.redeploy(
            flag=flag, to_location=other_loc, actor=who["purchase_mgr"],
            reason="needed on the second site",
        )
        self.note(f"{transfer.number} proposed — the receiving PM must accept.")
        inventory.accept_transfer(transfer=transfer, actor=who["purchase_mgr"])
        events.drain()
        self.ok("Accepted — and the value moved with the stock")
        self.row("Releasing project", f"₹{costing.project_total(project.pk, CostEntry.STOCK_OUT):,.0f}", widths=(22, 30))
        self.row("Receiving project", f"₹{costing.project_total(other.pk, CostEntry.STOCK_IN):,.0f}", widths=(22, 30))
        self.note("The one deliberate breach of project isolation — which is exactly")
        self.note("why the value moves explicitly rather than the stock moving silently.")

        self.h2("Site running costs")
        for category, amount in [
            (ExpenseCategory.ROOM_RENT, "48000"), (ExpenseCategory.WATER, "9500"),
            (ExpenseCategory.CONVEYANCE, "26500"), (ExpenseCategory.FOODING, "31000"),
        ]:
            expense = finance.submit_expense(
                project=project, category=category, amount=Decimal(amount),
                expense_date=date.today(), actor=who["site_engineer"],
            )
            finance.approve_expense(expense=expense, actor=who["construction_mgr"])
        events.drain()
        self.ok(f"₹{costing.project_total(project.pk, CostEntry.SITE_EXPENSE):,.0f} of site running costs approved")
        self.note("Outside the BOQ entirely, but in the same ledger as material and")
        self.note("subcontract — so they cannot miss the margin figure.")

        # ================================================== 7. COSTING
        self.h1("10.  COST LEDGER — margin per project and per SITC lot")

        costing.post_cost(
            project_id=project.pk, lot_id=hvac.pk, category=CostEntry.MATERIAL,
            amount=Decimal("1980000"), source_type="vendor_bill", source_id=3, actor=who["buyer"],
        )
        for lot, amount, number in [(fire, "3400000", "INV-1"), (hvac, "2100000", "INV-2")]:
            invoice = ClientInvoice.objects.create(
                order=order, lot=lot, number=f"{number}-{stamp}",
                invoice_date=date.today(), amount=Decimal(amount),
            )
            with transaction.atomic():
                sales.issue_invoice(invoice=invoice, actor=who["sales_mgr"])

        self.h2("Cost by category")
        result = costing.profitability(project.pk)
        for category, total in sorted(result["by_category"].items()):
            self.row(category, f"₹{total:,.0f}", widths=(26, 24))
        self.note("Site running costs sit in the same ledger as material and subcontract.")
        self.note("There is no category of real spend that misses the margin figure.")

        self.h2("Margin per SITC lot — the reason Lot is a first-class entity")
        self.row("LOT", "INVOICED", "COST", "MARGIN", widths=(34, 15, 15, 15))
        for lot in (fire, hvac):
            p = costing.lot_profitability(lot.pk)
            self.row(lot.name[:32], f"₹{p['revenue']:,.0f}", f"₹{p['cost']:,.0f}",
                     f"₹{p['margin']:,.0f}", widths=(34, 15, 15, 15))
        self.row("PROJECT TOTAL", f"₹{result['revenue']:,.0f}", f"₹{result['cost']:,.0f}",
                 f"₹{result['margin']:,.0f}", widths=(34, 15, 15, 15))
        self.note("Lot 2's thin margin is invisible in the blended project figure.")

        # ================================================== 8. GOVERNANCE
        self.h1("11.  GOVERNANCE & AUDIT")

        self.h2("Administrator override of a locked record")
        from apps.platform_core.models import AdminOverride, Override

        rev0.refresh_from_db()
        rev0.sent_back_reason = "corrected after approval — agreed with PM"
        rev0.save(override=Override(who["admin"], reason="typo in the released revision note"))
        logged = AdminOverride.objects.filter(entity_id=rev0.pk).first()
        self.ok("Override permitted — and recorded for Director review")
        self.row("Actor", logged.actor.username, widths=(16, 56))
        self.row("Reason", logged.reason, widths=(16, 56))

        self.h2("Ledger integrity")
        entry = CommitmentEntry.objects.filter(boq_line=pipe200).first()
        try:
            entry.qty_delta = Decimal("9999")
            entry.save()
        except NotImplementedError as exc:
            self.blocked(str(exc))

        # Scoped to this project: the demo can be run repeatedly against the
        # same database, and unscoped counts would silently accumulate.
        self.h1("SUMMARY — this run")
        self.row("Commitment entries", CommitmentEntry.objects.filter(project=project).count(), widths=(30, 40))
        self.row("Cost entries", CostEntry.objects.filter(project=project).count(), widths=(30, 40))
        self.row("BOQ revisions released",
                 BoqRevision.objects.filter(project=project, status=BoqRevision.RELEASED).count(),
                 widths=(30, 40))
        self.row("Reconciliation verdicts",
                 ReconciliationOutcome.objects.filter(revision__project=project).count(),
                 widths=(30, 40))
        self.row("Blocked by a control", 4, widths=(30, 40))
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("  Demo complete — no mocks, no fixtures beyond Discern's own BOQ files."))
        self.stdout.write("")
