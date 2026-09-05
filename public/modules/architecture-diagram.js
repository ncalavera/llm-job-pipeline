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
    DB --> SCORE[Filter +<br/>AI scoring]
    SCORE --> YOU[Dashboard / Telegram<br/>you triage]
    YOU -->|likes & passes| LEARN[Learning loop]
    LEARN -->|tunes filters| DB
    DB --> OBS[Health tab +<br/>run report card]

    style DB fill:#1E40AF,color:#fff
    style SCORE fill:#065F46,color:#fff
    style YOU fill:#7C2D12,color:#fff
    style LEARN fill:#4C1D95,color:#fff
    style OBS fill:#0F766E,color:#fff
`;

export const ARCHITECTURE_DETAILS = [
  {
    id: "archDailyRun",
    title: "Daily run — stage by stage",
    src: `flowchart LR
    V[validate profile] --> P[preflight DB check] --> LR2[learning review]
    LR2 --> F[fetch: career sites + boards] --> EN[enrich blind roles]
    EN --> FI[filter junk] --> CS[company scoring] --> VS[vacancy scoring<br/>cheap screen, then strong model]
    VS --> SP[screening prep<br/>facts + quotes, no score, night only]
    SP --> VD[your verdicts] --> PU[publish snapshot once]

    style F fill:#1E40AF,color:#fff
    style VS fill:#065F46,color:#fff
    style SP fill:#065F46,color:#fff
    style VD fill:#7C2D12,color:#fff
`,
  },
  {
    id: "archMoneyValve",
    title: "Company scoring — where money is spent, and the valve",
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
