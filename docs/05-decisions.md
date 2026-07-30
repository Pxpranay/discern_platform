# Decisions Register

Rev. 17 ended with **39 open questions**, all presented as prerequisites. That is too many to gate a build, and treating them as equally blocking is how a design stalls indefinitely at the approval stage.

They are triaged here into three tiers. **Eight block the build.** Everything else has a recommended default that can be adopted now and revisited during rollout without holding up a line of code.

Every deferred item carries a recommendation, so the answer to each is "yes, that default is fine" or "no, here's ours" — not an open essay question.

---

## Tier 1 — Blocking. These change the schema.

Needed before **Phase 2** (items 1, 2, 6) or **Phase 1** (items 7, 8). Items 3, 4, 5 are needed before Phase 3.

### 1. Does the quantity ceiling allow a wastage tolerance?
Cement, tiles, sand and similar consumables are genuinely consumed above their measured quantity through breakage and wastage. Should the ceiling permit a percentage above the BOQ figure, or is every unit above it a formal revision?

**Recommendation:** allow a tolerance, configured **per item category**, defaulting to 0%. Set 3–5% on consumables and 0% on discrete items (a pump, a fabricated staircase). A global percentage is wrong in both directions — too loose on equipment, too tight on cement.

**Why it blocks:** `headroom = quantity + tolerance − committed` requires knowing whether `tolerance` exists and what it attaches to.

### 2. Does the Project Manager get an emergency ceiling override?
For a genuine site emergency — urgent material to prevent damage or delay, with no time to run a revision approval.

**Recommendation:** **yes**, with hard conditions: PM-only, mandatory reason, capped at a configurable value, and it creates an **obligation** — the project cannot release its next BOQ revision until every override is reconciled into it. Overrides appear on the Directors' dashboard.

**Why it blocks:** adds an authorization path through `reserve_headroom` and a blocking obligation on revision release. Bolting this on later means revisiting the one function the whole design's integrity rests on.

**The alternative is worse.** A ceiling with no escape hatch will be worked around — material bought on someone's personal account, entered later as something it was not. A logged override is strictly better than an invisible one.

### 3. Is fabrication in-house, job-worked, or both?
**Recommendation:** support **both** from the start (`fabrication_order.mode`). Job work issues components to a vendor location and receives the finished item back. If Discern only does one today, the other will appear within a year, and the stock flow differs enough that retrofitting it is real work.

**Why it blocks:** job work needs vendor locations, component issue-out moves, and a subcontract cost component on a fabrication order.

### 4. What value basis for inter-project stock transfer?
Original purchase cost, current replacement cost, or a figure the Purchase Manager sets case by case?

**Recommendation:** **original purchase cost**, taken from the receipt the stock came in on. It is factual, needs no judgement, and keeps both projects' books reconcilable to actual spend. Replacement cost invents a gain or loss neither project caused.

**Why it blocks:** original cost requires tracking valuation **per receipt** rather than a moving average per item — a schema decision in the stock ledger.

### 5. Is each site a separate warehouse, or a location under a central yard?
**Recommendation:** a **location hierarchy** — one root per company yard, one child location per project site, with `location.project` set. This gives project isolation and yard-to-site transfer tracking without the overhead of full warehouse records per site, and the hierarchy scales to whatever Discern needs later.

**Why it blocks:** shapes the location tree and every availability query written against it.

### 6. Who signs off a BOQ section?
Is the preparing Manager's own confirmation that their section is complete sufficient, with the Project Manager's release as the only independent check — or does a separate Design Head / Construction Head sign off first?

**Recommendation:** the **preparing Manager's own confirmation**, with the PM's release as the independent check. A third tier on every revision, on a document expected to revise many times per project, will become a rubber stamp and add days to every cycle. If a project is large enough to warrant more scrutiny, the Director threshold on release already covers it.

**Why it blocks:** changes the states and roles on `boq_section`.

### 7. Multi-currency or import procurement?
**Recommendation:** if any material is imported or any vendor invoices in foreign currency, say so now. Retrofitting currency into a cost ledger is expensive; designing for it in Phase 1 is nearly free.

**Why it blocks:** `cost_entry.amount` needs a currency and a rate at posting, or it does not.

### 8. What must this integrate with?
Statutory accounting (Tally, Zoho, other), GST filing, banking, existing document storage?

**Recommendation:** define the **export boundary** now even if the integration is built in Phase 5. Process §1.1 deliberately excludes statutory books; something must consume the cost and revenue position, and knowing what determines whether an integration module is scoped early or late.

**Why it blocks:** if a statutory system needs voucher-level detail, the cost ledger's shape must accommodate it from the start.

---

## Tier 2 — Policy. Configurable; recommended defaults.

These are real decisions with real consequences, but each is a configuration value or a rule the platform reads rather than a schema change. Adopt the defaults and refine in rollout.

| Question | Recommendation |
|---|---|
| Who holds the Sales-to-Project kickoff approval? | Project Manager, with the Sales Manager as an alternate. Value-threshold-free — the check is fast and every order benefits |
| What value triggers Purchase Order double approval, and who approves? | Set at a value that captures roughly the top 20% of orders by count. Purchase Manager role approves. Start higher than feels right and lower it — an approval queue nobody clears is worse than none |
| Does the ≥3-vendor rule apply at every value? | No. Below a low threshold a single trusted vendor is fine. A three-vendor exercise on a ₹4,000 purchase costs more than it saves |
| Single-source item with fewer than three possible quotes? | Proceed with a **recorded reason** (`rfq.min_vendors_waived_reason`). Blocking indefinitely punishes the buyer for market reality |
| Must the Purchase Manager justify awarding above the lowest quote? | **No** — this was stated as "totally his prerogative" and the design honours it. The frozen `comparison_snapshot` is the audit trail, which is stronger than a free-text reason nobody reads |
| Should a receipt discrepancy hold the vendor bill? | **Yes**, hold automatically until resolved. Cost must not enter a project on an unverified delivery — releasing first and correcting later is how bad numbers become permanent |
| Does a return on an already-invoiced order need Finance sign-off? | Yes, above the same threshold as purchase approval. Below it, Purchase actions directly |
| Should a BOQ decrease on received material return to vendor or redeploy? | **Offer both** at the resolution step. Redeployment to a project that needs it is usually better commercially than a vendor return with a restocking cost |
| Who certifies service completion — Site Engineer, or someone else? | Site Engineer certifies quantity; **Construction Manager** certifies value on the running bill. Splitting them means the person confirming work happened is not the person confirming what it is worth |
| Is partial (running-bill) service completion allowed? | **Yes** — it is the norm for subcontractors, not an edge case |
| Is per-lot margin standard reporting or on request? | **Standard.** The lot is a first-class entity (process §3.2), so it costs nothing extra to show |
| Which items are `FABRICATE` vs `SUPPLY`? | A one-time item-master exercise. Default everything to `SUPPLY`; the Design Manager can only tag `FABRICATE` on items with a bill of materials |
| Should every service line skip RFQ, or only empanelled vendors? | **Only empanelled vendors with an agreed rate.** New or one-off subcontractors route through normal RFQ — that is exactly when price discovery has value |
| Who counts as a Construction User, and do they need full accounts? | Site supervisors and coordinators, on **mobile-first screens** rather than the full back office. Full accounts, restricted role, project-scoped |
| Should Directors/Finance see per-project dashboards or only portfolio? | Both, read-only. There is no reason to withhold a project view from a Director |
| Should the Procurement Manager see BOQ/site-progress tiles? | Yes, for their assigned projects. A buyer who can see BOQ status makes better sourcing decisions |
| Is the site-progress figure the site-reported one, or a separate PM figure? | **One figure**, site-reported, with certification as the control. Two competing progress numbers means neither is trusted |
| Who submits site expense claims? | The site coordinator on behalf of the site, for shared costs (room rent, water). Individuals for their own conveyance |
| Does the Construction Manager approve site expenses before they appear? | **No** — show them immediately as `pending`, include them in the comparison sheet flagged as unapproved. Hiding real spend until it is approved makes the margin figure optimistic exactly when it matters |
| Should the expense-vs-income sheet be running total, period, or both? | **Both**, toggleable. Same query, different date filter |
| How many schedule phases by default? | Four (site visit, BOQ prep, one procurement stage, construction), with procurement stages addable per project. No configuration needed — they are just rows |
| Must a schedule extension carry a client confirmation document? | **Yes**, mandatory attachment (`schedule_extension.client_agreement_reference`). This is a contractual date; CEO sign-off alone is not evidence the client agreed |
| Should Purchase see site-raised requisitions as flagged? | **Yes** (`is_site_raised`). Urgency and provenance both matter to a buyer |
| How fast must stock availability refresh? | **Live.** It is a query over the ledger; there is no reason to cache it into staleness |
| Should availability include stock reserved against other projects? | Show both: free and reserved, in separate columns. The Purchase Manager can then judge |
| Who decides where flagged excess stock goes? | Purchase Manager decides; **receiving Project Manager must accept**. Cost is landing on the receiving project's books, so its owner gets a say |
| Should Finance review inter-project reallocations periodically? | Yes, monthly. They do not clear through a vendor bill, so they get no natural review |
| Should the warehouse stock dashboard be Purchase-Manager-only? | No — PMs see their own project's slice; the Purchase Manager sees everything |
| Can a user hold more than one module role? | **Yes.** Roles compose (process §5.5). One role per user does not survive a real org chart |
| Is "approved and forwarded" the exact locking moment, or is there a grace window? | **Exact, no grace window** — but make send-back easy before approval and amendment easy after. A grace window is a second, undocumented state that every downstream reader must now handle |
| Who holds the Administrator role day to day? | A **designated System Administrator, distinct from the CEO**, with every override logged and visible to the Directors. The CEO should not be doing data fixes, and the reviewer of overrides should not be the person making them |

---

## Tier 3 — Already answered by the design.

Rev. 17 raised these as open. The redesign settles them structurally, and no decision is required.

| Question | Settled by |
|---|---|
| Does a BOQ need multiple revisions, or is one working version enough? | **Multiple, always.** `boq_revision` is the model (process §3.3). Construction BOQs are never final on day one |
| Should the BOQ-to-order-line link be mandatory or optional? | **Always mandatory.** `lot` is a first-class entity, and every BOQ line carries one (process §3.2) |
| Is subcontractor work in scope for this flow or handled separately? | **In scope**, as the `SERVICE` route with its own order, certification and billing path (process §4.10) |
| What level of site data entry is realistic? | **Mobile-first screens** for every site role, offline-tolerant (process §5.5). Assuming a site desktop is how site data stops arriving |
| Should the earlier design's two BOQ approvals both be required? | **Superseded.** One revision, two discipline-owned sections, empty sections auto-complete (process §3.4, §9.1) |
| Do the Design and Construction Managers need visibility into each other's BOQ portion? | **Yes, by construction** — it is one revision, and both see all of it while preparing (process §4.4) |

---

## Answering This Register

The eight Tier 1 items are the ones to bring to a decision meeting. For Tier 2, the useful review is a read-through marking only the rows Discern disagrees with — the defaults are chosen to be defensible, not merely to fill the table.

Nothing in Tier 3 needs discussion unless the reasoning given is wrong, in which case that is worth knowing before Phase 2.
