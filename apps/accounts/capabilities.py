"""The capability catalogue.

One registry, so the admin screen can offer a checklist of what actually exists
rather than a free-text box where a typo silently grants nothing. A capability
string that is not in here is a bug, and the admin screen will not show it.

Two kinds:

* **View** capabilities gate a whole screen. Without one, the nav item is not
  shown and the URL returns "not permitted" — hiding the link alone is not
  access control.
* **Action** capabilities gate a single operation inside a screen the user can
  otherwise see: a Purchase Manager may open a project without being able to
  extend its committed delivery date.
"""

VIEW = "view"
ACTION = "action"

#: (code, label, group, kind)
CAPABILITIES: list[tuple[str, str, str, str]] = [
    ("dashboard:view", "See the portfolio dashboard", "Dashboard", VIEW),
    ("crm:view", "See leads and the opportunity pipeline", "CRM", VIEW),
    ("sales:view", "See orders and lots", "Sales", VIEW),
    ("order:confirm", "Confirm an order and set its committed delivery date", "Sales", ACTION),
    ("order:approve_kickoff", "Approve a confirmed order for project kickoff", "Sales", ACTION),
    ("projects:view", "See projects, schedules and margin", "Projects", VIEW),
    ("project:plan_schedule", "Add and re-plan master schedule phases", "Projects", ACTION),
    ("project:extend_schedule", "Extend the committed delivery date (CEO / PM only)", "Projects", ACTION),
    ("boq:view", "See BOQ revisions and reconciliation", "BOQ", VIEW),
    ("boq:edit", "Add or change lines on a draft revision", "BOQ", ACTION),
    ("boq:sign_off", "Sign off a discipline section", "BOQ", ACTION),
    ("boq_revision:release", "Release a revision downstream (Project Manager)", "BOQ", ACTION),
    ("procurement:view", "See procurement requests, RFQs and purchase orders", "Procurement", VIEW),
    ("procurement:request", "Raise a site requisition", "Procurement", ACTION),
    ("procurement:approve_request", "Approve a site-raised requisition (Project Manager)", "Procurement", ACTION),
    ("procurement:rfq", "Create and issue an RFQ, and record quotes", "Procurement", ACTION),
    ("procurement:award", "Award a line to a vendor, irrespective of price (Purchase Manager)", "Procurement", ACTION),
    ("purchase_order:create", "Raise a purchase order from awarded lines", "Procurement", ACTION),
    ("purchase_order:approve", "Approve an order above the value threshold (Purchase Manager)", "Procurement", ACTION),
    ("receipt:view", "See expected and recorded goods receipts", "Receipts", VIEW),
    ("receipt:record", "Record what physically arrived (Store Keeper)", "Receipts", ACTION),
    ("receipt:verify", "Verify quantity and quality, accepting cost (Site Engineer)", "Receipts", ACTION),
    ("receipt:return", "Return material to a vendor", "Receipts", ACTION),
    ("works:view", "See fabrication orders and subcontract service orders", "Works", VIEW),
    ("fabrication:manage", "Raise and run fabrication orders", "Works", ACTION),
    ("service_order:issue", "Issue a service order to a subcontractor", "Works", ACTION),
    ("service_order:progress", "Log day-to-day progress against BOQ scope", "Works", ACTION),
    ("service_order:certify", "Certify completed work as billable", "Works", ACTION),
    ("expenses:view", "See site expenses", "Site expenses", VIEW),
    ("expenses:submit", "Submit a site expense claim", "Site expenses", ACTION),
    ("expenses:approve", "Approve a site expense", "Site expenses", ACTION),
    ("stock:flag_excess", "Flag received stock as dead or available elsewhere", "Receipts", ACTION),
    ("stock:transfer", "Redeploy stock between projects", "Receipts", ACTION),
    ("admin:manage", "Manage users, roles and project assignments", "Administration", VIEW),
]

ALL_CODES = {code for code, _, _, _ in CAPABILITIES}


def grouped() -> dict[str, list[dict]]:
    """Capabilities by group, for the role editor."""
    out: dict[str, list[dict]] = {}
    for code, label, group, kind in CAPABILITIES:
        out.setdefault(group, []).append(
            {"code": code, "label": label, "kind": kind, "is_view": kind == VIEW}
        )
    return out


def label_for(code: str) -> str:
    for c, label, _, _ in CAPABILITIES:
        if c == code:
            return label
    return code


def unknown(codes) -> set[str]:
    """Codes not in the catalogue — surfaced rather than silently accepted."""
    return set(codes) - ALL_CODES


#: Roles created by ``seed_roles``. These mirror the roles in the process
#: design's "Roles Along the Chain" table, so a fresh install starts with
#: something recognisable rather than an empty permissions screen.
DEFAULT_ROLES: list[tuple[str, str, list[str]]] = [
    ("administrator", "Administrator", sorted(ALL_CODES)),
    ("director", "Director / Finance", ["dashboard:view", "crm:view", "sales:view", "projects:view", "boq:view", "procurement:view", "receipt:view"]),
    ("sales_rep", "Sales Representative", ["crm:view", "sales:view"]),
    ("sales_manager", "Sales Manager", ["crm:view", "sales:view", "order:confirm", "order:approve_kickoff", "dashboard:view"]),
    ("project_manager", "Project Manager", [
        "dashboard:view", "projects:view", "boq:view", "sales:view",
        "project:plan_schedule", "project:extend_schedule", "boq_revision:release",
        "order:approve_kickoff", "procurement:view", "procurement:approve_request",
        "receipt:view", "works:view", "expenses:view", "expenses:approve", "stock:transfer",
    ]),
    ("design_manager", "Design Manager", [
        "projects:view", "boq:view", "boq:edit", "boq:sign_off", "works:view", "fabrication:manage",
    ]),
    ("construction_manager", "Construction Manager", ["projects:view", "boq:view", "boq:edit", "boq:sign_off", "procurement:request", "procurement:view", "works:view", "service_order:issue",
        "service_order:progress", "service_order:certify", "expenses:view", "expenses:approve"]),
    ("purchase_manager", "Purchase Manager", [
        "dashboard:view", "projects:view", "boq:view", "procurement:view", "receipt:view",
        "procurement:rfq", "procurement:award", "purchase_order:create", "purchase_order:approve",
        "stock:transfer", "works:view",
    ]),
    ("procurement_officer", "Procurement Officer", [
        "projects:view", "boq:view", "procurement:view", "receipt:view",
        "procurement:rfq", "purchase_order:create",
    ]),
    ("store_keeper", "Store / Site Keeper", ["projects:view", "receipt:view", "receipt:record"]),
    ("site_engineer", "Site Engineer", [
        "projects:view", "boq:view", "receipt:view", "receipt:verify", "receipt:return",
        "procurement:request", "works:view", "service_order:progress",
        "stock:flag_excess", "expenses:view", "expenses:submit",
    ]),
]
