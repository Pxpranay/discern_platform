# Process Flowcharts

Companion to [`01-process-design.md`](01-process-design.md). Rendered natively by GitHub.

**Deliberately split into eight focused diagrams rather than one.** Rev. 17's single flowchart spanned three pages at a scale where the box text was unreadable, and its own document had to add a note pointing to a separate zoomable HTML file. A diagram nobody can read is not documentation. Each diagram below fits on one screen and was render-checked at readable scale.

**Legend, consistent across all six:**

| Style | Meaning |
|---|---|
| Rectangle | Process step |
| Diamond | Decision or approval gate — a named person signs off |
| `▸ AUTO` | Automatic hand-off, no re-typing (a durable event, process §5.3) |
| `▪ LEDGER` | Posts to a ledger — the authority for cost, commitment or stock |

---

## 1. End to End

```mermaid
flowchart TD
    A["Enquiry / Lead"] --> B["Opportunity<br/>site visit + estimate"]
    B --> C{"Won?"}
    C -->|No| B
    C -->|Yes| D["Quotation<br/>priced by Lot"]
    D --> E["Order Confirmed<br/>committed delivery date fixed"]
    E --> F{"Approved for<br/>kickoff?<br/>PM / Sales Mgr"}
    F -->|Hold| E
    F -->|Yes| G["▸ AUTO<br/>Project + cost scope<br/>+ site location created"]
    G --> H["Master Schedule<br/>planned by CEO / PM"]
    H --> I{"Within committed<br/>delivery date?"}
    I -->|"No"| H2["Client agrees extension<br/>then re-plan"]
    H2 --> I
    I -->|Yes| J["BOQ Revision prepared<br/>Goods section + Service section"]
    J --> K{"Both sections<br/>signed off?"}
    K -->|No| J
    K -->|Yes| L{"Released by<br/>Project Manager?"}
    L -->|"Send back"| J
    L -->|Yes| M["▸ AUTO<br/>Reconcile vs ledgers<br/>forward deltas only"]
    M --> N["Procurement Request"]
    N --> O{"Route?"}
    O -->|SUPPLY| P["Purchase Order<br/>→ Receipt → Verify"]
    O -->|FABRICATE| Q["Fabrication Order<br/>→ Produce"]
    O -->|SERVICE| R["Service Order<br/>→ Certify"]
    P --> S["▪ LEDGER<br/>Cost Ledger"]
    Q --> S
    R --> S
    T["Site Expenses"] --> S
    U["Client Invoices"] --> S
    S --> V["Profitability<br/>per project and per lot"]

    classDef gate fill:#fff8e1,stroke:#f57f17,stroke-width:2px
    classDef auto fill:#1a237e,stroke:#1a237e,color:#fff
    classDef ledger fill:#004d40,stroke:#004d40,color:#fff
    class C,F,I,K,L,O gate
    class G,M auto
    class S,V ledger
```

---

## 2. Enquiry to Project Initiation

```mermaid
flowchart TD
    A["Lead captured<br/>enquiry / referral / tender"] --> B["Auto-assigned<br/>by territory or work type"]
    B --> C["Site Visit<br/>+ Technical Estimate"]
    C --> D{"Qualified?"}
    D -->|No| E["Nurture"]
    E --> C
    D -->|Yes| F["Opportunity<br/>rough order value"]
    F --> G{"Won?"}
    G -->|No| H["Lost<br/>reason recorded"]
    G -->|Yes| I["Quotation drafted"]

    I --> J["Lots defined"]
    J --> J1["Itemized lot<br/>lines + rates"]
    J --> J2["Lump-sum SITC lot<br/>single price, no breakdown"]

    J1 --> K["Client accepts"]
    J2 --> K
    K --> L["Order Confirmed<br/>price per lot<br/>+ committed delivery date"]
    L --> M{"Approved for kickoff?<br/>PM / Sales Manager"}
    M -->|"No / Hold"| N["Held Pending Review<br/>re-checked, not lost"]
    N --> M
    M -->|Yes| O["▸ AUTO<br/>Project created<br/>lots copied<br/>cost scope opened<br/>site location provisioned"]

    O --> P["Master Schedule planned<br/>CEO / Project Manager"]
    P --> P1["Site Visit phase"]
    P --> P2["BOQ Preparation phase"]
    P --> P3["Procurement phases<br/>as many as phased delivery needs"]
    P --> P4["Construction phase"]

    P1 --> Q{"All phases within<br/>committed delivery date?"}
    P2 --> Q
    P3 --> Q
    P4 --> Q
    Q -->|"No — exceeds"| R["Blocked.<br/>Client must agree<br/>a later delivery date"]
    R --> S["Extension recorded<br/>client agreement attached<br/>CEO / PM authorizes"]
    S --> P
    Q -->|Yes| T["Schedule live<br/>→ PM Dashboard"]

    classDef gate fill:#fff8e1,stroke:#f57f17,stroke-width:2px
    classDef auto fill:#1a237e,stroke:#1a237e,color:#fff
    classDef block fill:#ffebee,stroke:#c62828,stroke-width:2px
    class D,G,M,Q gate
    class O auto
    class R block
```

**Note the one-way boundary.** The order fixes what the client pays and when they get it. Neither figure is ever changed by anything downstream — only a formal change order through Sales moves them.

---

## 3. BOQ Preparation, Release & Reconciliation

```mermaid
flowchart TD
    A["Project drawings<br/>+ physical site requirement"] --> B["BOQ Revision opened<br/>Rev 1, 2, 3 ..."]

    B --> C["Goods Section<br/>Design Manager"]
    B --> D["Service Section<br/>Construction Manager"]

    C --> C1["Materials + fabricated items<br/>each tagged SUPPLY or FABRICATE<br/>each carrying its Lot"]
    D --> D1["Subcontract / execution scope<br/>route = SERVICE<br/>each carrying its Lot"]

    C1 --> E{"Goods section<br/>signed off?<br/>or marked<br/>not applicable"}
    D1 --> F{"Service section<br/>signed off?<br/>or marked<br/>not applicable"}

    E -->|No| C1
    F -->|No| D1

    E -->|Yes| G{"Released by<br/>Project Manager?<br/>Director above threshold"}
    F -->|Yes| G

    G -->|"Send back<br/>with comments"| B
    G -->|Yes| H["▸ AUTO<br/>Revision locked.<br/>Reconciliation engine runs<br/>against commitment + stock ledgers"]

    H --> I{"Net change<br/>per line?"}

    I -->|"New line or<br/>qty increased"| J["Delta only<br/>→ Procurement Request"]
    I -->|"Decreased,<br/>nothing committed"| K["Draft request line<br/>reduced or removed.<br/>Nobody notified."]
    I -->|"Decreased,<br/>ordered not delivered"| L["Amend or cancel<br/>open order qty"]
    I -->|"Decreased,<br/>already received"| M["Return / Redeployment<br/>queue"]
    I -->|"Unchanged"| N["No action"]

    L --> O["▪ LEDGER<br/>Commitment nets down"]
    M --> M1{"Return to vendor<br/>or redeploy to<br/>another project?"}
    M1 -->|Return| M2["Return + debit note"]
    M1 -->|Redeploy| M3["Internal transfer<br/>see diagram 7"]
    M2 --> O
    M3 --> O

    classDef gate fill:#fff8e1,stroke:#f57f17,stroke-width:2px
    classDef auto fill:#1a237e,stroke:#1a237e,color:#fff
    classDef ledger fill:#004d40,stroke:#004d40,color:#fff
    class E,F,G,I,M1 gate
    class H auto
    class O ledger
```

**Two things this diagram fixes from rev. 17.** An empty section is marked *not applicable* and releases normally, so a materials-only or labour-only project cannot deadlock waiting on a signature. And reconciliation reads the **ledgers**, not the previous revision's text — so it stays correct when a revision lands while orders are already in flight, which on a live project is the normal case.

---

## 4. Procurement & Material Receipt

```mermaid
flowchart TD
    B["Procurement Request<br/>site-raised flag preserved"]

    subgraph SRC["Three sources, one record"]
        A1["BOQ release<br/>reconciliation delta"]
        A2["Site requisition<br/>raised by Construction team"] --> A2G{"Project Manager<br/>approves?"}
        A2G -->|"No / Hold"| A2P["Parked for re-review"]
        A2P --> A2G
        A3["Fabrication shortfall<br/>missing raw material"]
    end

    A1 --> B
    A2G -->|Yes| B
    A3 --> B

    B --> C["Stock Availability<br/>on-hand in every location<br/>+ last purchase price and date"]

    C --> D{"Purchase Manager:<br/>use existing stock<br/>or buy new?<br/>sole discretion"}
    D -->|"Use existing"| E["Internal Transfer<br/>from holding location"]
    E --> Z["▪ LEDGER<br/>Stock Ledger"]

    D -->|"Buy new"| F["RFQ issued<br/>to at least 3 vendors"]
    F --> G{"3 or more<br/>responses?"}
    G -->|"No"| G1["Proceed only with<br/>recorded waiver reason"]
    G1 --> H
    G -->|Yes| H["Comparison Statement<br/>price, unit price, delivery, terms<br/>best highlighted as information only"]

    H --> I["Award<br/>Purchase Manager selects any vendor,<br/>irrespective of quoted price.<br/>Comparison snapshot frozen."]

    I --> J{"Within BOQ<br/>headroom?"}
    J -->|"No — blocked"| J1["Rejected with reason.<br/>New BOQ revision required."]
    J -->|Yes| K["▪ LEDGER<br/>Commitment posted"]
    K --> L{"Value above<br/>approval threshold?"}
    L -->|Yes| M["Purchase Manager approves"]
    L -->|No| N
    M --> N["Purchase Order live<br/>locked on approval"]

    N --> O["▸ AUTO<br/>Expected receipt created<br/>at project site location<br/>SUPPLY lines only"]
    O --> P["Goods Receipt<br/>Store / Site Keeper"]
    P --> Q{"Qty and quality<br/>verified?<br/>Site Engineer"}

    Q -->|"Mismatch"| R["Discrepancy logged<br/>+ photographs<br/>→ vendor debit note<br/>→ replacement request<br/>headroom released"]
    R --> B
    Q -->|Match| S["▪ LEDGER<br/>Stock posted<br/>+ MATERIAL cost posted<br/>+ vendor bill cleared"]

    classDef gate fill:#fff8e1,stroke:#f57f17,stroke-width:2px
    classDef auto fill:#1a237e,stroke:#1a237e,color:#fff
    classDef ledger fill:#004d40,stroke:#004d40,color:#fff
    classDef block fill:#ffebee,stroke:#c62828,stroke-width:2px
    class A2G,D,G,J,L,Q gate
    class O auto
    class K,S,Z ledger
    class J1 block
```

**Nothing enters a project's cost on an unverified delivery.** Verification is a separate act by a separate role from receipt — that is why they are two records, not two checkboxes on one.

---

## 5. FABRICATE Route

```mermaid
flowchart TD
    A["BOQ line, route = FABRICATE<br/>made to project drawings"] --> B{"Within BOQ<br/>headroom?"}
    B -->|"No — blocked"| B1["Rejected.<br/>New BOQ revision required."]
    B -->|Yes| C["▪ LEDGER<br/>Commitment posted<br/>on finished item"]
    C --> D["Fabrication Order<br/>against Bill of Materials<br/>mode: in-house or job work"]
    D --> E{"Raw materials<br/>in stock?"}
    E -->|"No — shortfall"| F["Child Procurement Requests<br/>for the missing materials only.<br/>Deliberately not ceiling-checked:<br/>finished qty already capped upstream."]
    F --> G["Normal procurement<br/>→ diagram 4"]
    G --> E
    E -->|Yes| H["Work steps executed"]
    H --> I["Consumption recorded<br/>planned vs actual —<br/>over-consumption is visible,<br/>not absorbed"]
    I --> J["▪ LEDGER<br/>Finished item to project stock<br/>+ FABRICATION cost posted"]

    classDef gate fill:#fff8e1,stroke:#f57f17,stroke-width:2px
    classDef ledger fill:#004d40,stroke:#004d40,color:#fff
    classDef block fill:#ffebee,stroke:#c62828,stroke-width:2px
    class B,E gate
    class C,J ledger
    class B1 block
```

Downstream costing never needs to know whether an item was bought or built — both post to the same ledger against the same BOQ line.

---

## 6. SERVICE Route

```mermaid
flowchart TD
    A["BOQ line, route = SERVICE<br/>subcontract / execution scope"] --> B{"Vendor empanelled<br/>with an agreed rate?"}
    B -->|"No"| B1["Route through normal RFQ<br/>→ diagram 4.<br/>Price discovery has value<br/>for a new subcontractor."]
    B -->|Yes| C{"Within BOQ<br/>headroom?"}
    C -->|"No — blocked"| C1["Rejected.<br/>New BOQ revision required."]
    C -->|Yes| D["▪ LEDGER<br/>Commitment posted"]
    D --> E["Service Order direct to vendor<br/>scope, qty and price from BOQ.<br/>No RFQ round."]
    E --> F{"Above approval<br/>threshold?"}
    F -->|Yes| G["Purchase Manager approves.<br/>A direct service order is not<br/>a route around the threshold."]
    F -->|No| H["Order live, locked on approval"]
    G --> H
    H --> I["Progress logged against BOQ scope<br/>Project + Construction users<br/>percent complete, qty done"]
    I --> J{"Completion certified?<br/>Site Engineer on qty,<br/>Construction Mgr on value"}
    J -->|"Not yet"| I
    J -->|"Partial — running bill"| K
    J -->|"Final"| K["▪ LEDGER<br/>Vendor bill raised<br/>+ SUBCONTRACT cost posted.<br/>No goods receipt —<br/>nothing physical to receive."]
    K --> I

    classDef gate fill:#fff8e1,stroke:#f57f17,stroke-width:2px
    classDef ledger fill:#004d40,stroke:#004d40,color:#fff
    classDef block fill:#ffebee,stroke:#c62828,stroke-width:2px
    class B,C,F,J gate
    class D,K ledger
    class C1 block
```

Progressive billing is the loop from certification back to progress logging: a subcontractor bills each certified stage, and each stage posts independently.

---

## 7. Dead & Excess Stock Redeployment

```mermaid
flowchart TD
    A["Site Engineer or Site In-Charge<br/>flags a received line — at any time.<br/>Dead, or available for other project."] --> B["▸ AUTO<br/>Immediate fan-out to 3 dashboards:<br/>Project Manager, Construction Manager,<br/>Purchase Manager"]
    B --> C{"Purchase Manager:<br/>redeploy, or leave<br/>as dead stock?"}
    C -->|"Leave"| D["Remains flagged and visible.<br/>Not written off."]
    C -->|"Redeploy"| E{"Receiving Project Manager<br/>accepts?<br/>cost lands on their books"}
    E -->|No| D
    E -->|Yes| F["Internal Transfer<br/>between project locations"]
    F --> G["▪ LEDGER<br/>Stock moved"]
    G --> H["▪ LEDGER<br/>Paired cost entries:<br/>STOCK_OUT credits releasing project<br/>STOCK_IN debits receiving project"]
    H --> I["Both projects' profitability<br/>updated the same day"]

    classDef gate fill:#fff8e1,stroke:#f57f17,stroke-width:2px
    classDef auto fill:#1a237e,stroke:#1a237e,color:#fff
    classDef ledger fill:#004d40,stroke:#004d40,color:#fff
    class C,E gate
    class B auto
    class G,H ledger
```

**This is the one deliberate breach of project isolation** — which is exactly why it posts explicit paired cost entries rather than moving stock with no cost consequence. Note that flagged stock is never written off: relabelling it as usable elsewhere is the opposite of scrapping it.

---

## 8. Everything Converges on the Cost Ledger

```mermaid
flowchart LR
    A["Verified Goods Receipt"] -->|MATERIAL| L
    B["Fabrication completed"] -->|FABRICATION| L
    C["Service certified"] -->|SUBCONTRACT| L
    D["Site expenses<br/>room rent, water, conveyance,<br/>fooding, miscellaneous"] -->|SITE_EXPENSE| L
    E["Stock transferred out"] -->|STOCK_OUT| L
    F["Stock transferred in"] -->|STOCK_IN| L
    G["Client invoice raised"] -->|REVENUE| L

    L["▪ COST LEDGER<br/>append-only<br/>every entry carries<br/>project + lot + source document"]

    L --> M["Profitability<br/>per project"]
    L --> N["Profitability<br/>per SITC lot"]
    L --> O["Site Expense vs Income<br/>dated"]
    L --> P["Planned vs committed<br/>vs received vs billed"]
    L --> Q["Portfolio roll-up"]

    M --> R["PM Dashboard"]
    N --> R
    O --> R
    O --> S["Construction Mgr Dashboard"]
    P --> R
    Q --> T["Directors Dashboard"]

    classDef ledger fill:#004d40,stroke:#004d40,color:#fff,stroke-width:3px
    class L ledger
```

**There is no path by which real project spend avoids the profitability figure.** Site running costs post to the same ledger as material and subcontract cost — which is the structural fix for the gap rev. 17 documented in its §6.14, where an entire category of genuine spend could not reach the profitability panel at all and needed a separate report to see.

The Site Expense vs Income view on the Construction Manager's screen and on the Project Manager's dashboard is **one query with two viewers**. The numbers cannot disagree, because there is only one set of them.
