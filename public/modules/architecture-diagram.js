// =============================================================================
// architecture-diagram.js — the pipeline's data-flow picture, as Mermaid
// source strings rendered on the Health tab.
//
// Design FINDING-007: one 25-node diagram was unreadable. The canonical picture
// is now layered — a 6-node OVERVIEW that answers "how does a role travel from
// source to verdict" at a glance, plus per-component detail diagrams collapsed
// behind <details> and rendered only when opened.
//
// House rule (AGENTS.md): any change to the pipeline's shape updates BOTH this
// file and docs/ARCHITECTURE.md. Keep an explicit `color:` on every styled
// node so the labels stay legible in the dashboard's dark theme.
// =============================================================================

export const ARCHITECTURE_OVERVIEW = `flowchart LR
    SRC[Job boards +<br/>company career sites] -->|fetch daily| DB[(Database)]
    DB --> SCORE[Approved filters +<br/>score and facts]
    SCORE --> N[Cheap file-only agents<br/>one vacancy per request]
    N -->|score + quoted facts + comparison<br/>validated and saved by Python| DB
    DB -->|compact rows + filter facts| SCR[Scored Inbox table<br/>Score sort + reason batches<br/>Like / Pass + Undo]
    SCR -->|open one record| DETAIL[Full vacancy or company text]
    DB -->|private detail endpoint| DETAIL
    SCR -->|/api/screening-decision<br/>durable receipt| DB
    SCORE --> YOU[Dashboard / Telegram<br/>you review]
    YOU -->|Like / Pass| LEARN[Learning loop]
    LEARN -->|user-approved changes| DB
    SRC --> RAW[Algolia raw listing ledger<br/>before parser flags]
    RAW --> OBS[Sources: listing checks<br/>and collection gaps]
    DB --> OBS

    style DB fill:#1E40AF,color:#fff
    style SCORE fill:#065F46,color:#fff
    style SCR fill:#7C2D12,color:#fff
    style YOU fill:#7C2D12,color:#fff
    style LEARN fill:#4C1D95,color:#fff
    style OBS fill:#0F766E,color:#fff
`;

export const ARCHITECTURE_DETAILS = [
  {
    id: "archMaterials",
    title: "Private application materials",
    src: `flowchart LR
    SOURCES[Saved files + correspondence] --> IMPORT[Explicit import]
    IMPORT --> FILES[Private immutable files + catalogue]
    FILES --> API[Authenticated Materials page]
    DOSSIER[Private application notes and versions] --> EDITOR[Vacancy steps and history editor]
    EVENTS[Database status-change history] --> EDITOR
    FILES --> SEARCH[Agent keyword search for reuse]
    style FILES fill:#1E40AF,color:#fff
    style API fill:#065F46,color:#fff
`,
  },
  {
    id: "archDailyRun",
    title: "Daily run — stage by stage",
    src: `flowchart LR
    V[validate profile] --> P[preflight DB check] --> LR2[learning review]
    LR2 --> F[fetch: career sites + boards] --> EN[enrich blind roles]
    EN --> FI[filter junk] --> SP[combined discovery<br/>unscored: score + missing facts<br/>below 40: missing facts only<br/>40+: unchanged]
    SP --> TG[Inbox count + link] --> PU[publish snapshot once]
    PU --> VD[all retained vacancies in Inbox<br/>preparation never hides a role]
    F --> RAW[Algolia source observations<br/>raw listings + complete or partial run]

    style F fill:#1E40AF,color:#fff
    style SP fill:#065F46,color:#fff
    style VD fill:#7C2D12,color:#fff
`,
  },
  {
    id: "archMoneyValve",
    title: "Optional legacy company scoring — outside the daily path",
    src: `flowchart TB
    J[junk prefilter<br/>free] --> S[relevance screen<br/>cheap AI]
    S -->|pass + a vacancy scored 60+ or liked| U[website search + about scrape<br/>PAID Firecrawl]
    U --> E[evidence collection<br/>PAID Firecrawl + Exa]
    E --> W[WANT scoring]
    S -. screen crashed .-> X[VALVE CLOSED<br/>no paid steps this run]

    style S fill:#3730A3,color:#fff
    style U fill:#854D0E,color:#fff
    style E fill:#854D0E,color:#fff
    style X fill:#7F1D1D,color:#fff
`,
  },
  {
    id: "archObservability",
    title: "Trust & observability — how failures surface",
    src: `flowchart LR
    RS[run_state.json<br/>per-stage status + warnings] --> RC[report card<br/>after every run]
    RS --> PG{publish gate}
    PG -->|clean| PUB[dashboard updated]
    PG -->|dirty: stage error or screen crash| KEEP[previous snapshot kept]
    DB[(Database)] --> HD[api/health-detail.js] --> HT[Health tab<br/>this page]

    style RC fill:#854D0E,color:#fff
    style PG fill:#854D0E,color:#fff
    style DB fill:#1E40AF,color:#fff
    style HT fill:#0F766E,color:#fff
`,
  },
];
