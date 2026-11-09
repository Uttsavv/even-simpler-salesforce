# Architecture decision records

ADRs record the decisions that shape the library: what was decided, why, and what follows from
it. ADRs 0002–0011 summarise the decision log in §2 of the design spec
([`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md)),
grouped by topic. Later ADRs record decisions made during implementation. Where an ADR and the
spec disagree, the newer ADR wins and says what it supersedes.

| ADR | Decision | Status | Spec decisions |
|---|---|---|---|
| [0001](0001-retry-safety.md) | Only reads are resent after a request may have reached Salesforce | accepted | — |
| [0002](0002-new-library-and-scope.md) | A new, general-purpose library, shipped in stages | accepted | D1, D2, D3, D4, D5, D24, D25, O14, O15 |
| [0003](0003-async-first-with-generated-sync-twin.md) | Async first, with a generated sync twin | accepted | D6, O2, O5 |
| [0004](0004-one-transport-on-httpx.md) | One request pipeline on httpx; core depends on httpx only | accepted | D7, D8 (transport), D16, O7, O10 |
| [0005](0005-authentication-and-tokens.md) | Login flows as config objects; rotation-safe refresh | accepted | D8 (auth), D9, D10, O11 |
| [0006](0006-typed-errors.md) | Typed errors with one shape; opt-in tags; no client-side validation | accepted | D11, D12, D13 |
| [0007](0007-records-query-and-results.md) | Plain dicts, opaque cursors, cached describes | accepted | O6, O9, O12 |
| [0008](0008-observability-and-usage.md) | Canonical logs and free usage tracking | accepted | D15, D27 |
| [0009](0009-integrations-and-testing.md) | Framework integrations, testing support and quality gates | accepted | D17, D22, D28 |
| [0010](0010-org-automation-convert-and-merge.md) | Org automation, lead convert and merge | accepted | D14, D26 |
| [0011](0011-docs-versioning-and-releases.md) | Docs site, versioning and releases | accepted | D18, D19, D20, D21, D23, O1, O3, O4, O8, O13 |
| [0012](0012-request-headers.md) | Request headers are keyword arguments, scoped to the resources that document them | accepted | D30 |

To propose a change to one of these decisions, open an issue first (see `AGENTS.md`). A new ADR
gets the next number and is never rewritten after acceptance. If a later ADR replaces it, mark it
*superseded* and link the replacement.
