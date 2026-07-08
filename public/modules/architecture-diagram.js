// =============================================================================
// architecture-diagram.js — the pipeline's data-flow picture, as a Mermaid
// source string rendered on the Health tab.
//
// This is the ONE canonical diagram of how a role travels from source to
// verdict. It seeds from the flowchart in README.md and extends it with the
// pieces the trust/observability work added: the cheap relevance screen, the
// money valve that closes when the screen crashes, the learning loop, and the
// health/report-card observability surface.
//
// House rule (AGENTS.md): any change to the pipeline's shape updates BOTH this
// string and docs/ARCHITECTURE.md. Keep an explicit `color:` on every styled
// node so the labels stay legible in the dashboard's dark theme.
// =============================================================================

export const ARCHITECTURE_MERMAID = `flowchart TB
    A[ATS & job boards] -->|fetch| B[(Database)]
    A2[Company career sites] -->|enrich| B
    B -->|filter + quality gate| C[Clean vacancies]
    C -->|score, Claude| D[Scores 0-100]

    subgraph screen[Company scoring — paid valve]
        J[junk prefilter<br/>free] --> S[relevance screen<br/>Haiku, cheap]
        S -- pass + earned vacancy --> U[URL search + about scrape<br/>PAID]
        U --> E[evidence<br/>PAID]
        S -. crashed .-> X[VALVE CLOSED<br/>paid steps skipped]
    end
    B --> J
    E --> B

    D -->|archive low, optional| AR[Archive]
    D --> F[Dashboard / vac CLI / Telegram]
    F -->|triage| G{liked / passed /<br/>to_apply / applied}
    G -->|status| B
    G --> L[learning loop<br/>verdicts to filter/score]
    L -->|applied changes| B

    B --> H[api/health-detail.js]
    RS[run_state.json<br/>status + warnings] --> RC[run report card]
    RS --> PG[publish gate<br/>dirty = stage error or screen crash]
    H --> T[Health tab<br/>+ this diagram]

    style B fill:#1E40AF,color:#fff
    style D fill:#065F46,color:#fff
    style F fill:#7C2D12,color:#fff
    style S fill:#3730A3,color:#fff
    style X fill:#7F1D1D,color:#fff
    style L fill:#4C1D95,color:#fff
    style H fill:#0F766E,color:#fff
    style T fill:#0F766E,color:#fff
    style RC fill:#854D0E,color:#fff
    style PG fill:#854D0E,color:#fff
`;
