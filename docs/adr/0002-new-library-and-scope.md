# ADR 0002: A new, general-purpose library, shipped in stages

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions D1, D2, D3, D4, D5, D24, D25, O14, O15 (details in §1, §2, §19)

## Context

The Python options for Salesforce were a synchronous library built around SOAP login and
attribute access, and an async fork of it. Fixing their design problems (refresh that some calls
skip, errors you have to inspect by hand, partial failures that are easy to miss) would have
meant rewriting most of the code and breaking compatibility anyway.

## Decision

- Build a **new library** (D1) in its own public repository (D2), named `even-simpler-salesforce`,
  imported as `even_simpler_salesforce` and aliased `esf` in docs (D3).
- Keep it **general-purpose** (D4). Nothing specific to one app or org goes into the package.
- Aim for **full coverage of the modern Salesforce APIs** (D5), released in stages (D24): 0.1 core →
  0.2 files, actions, integrations → 0.3 Bulk, Tooling, GraphQL, Composite → 0.4 UI API, Reports,
  Metadata → 0.5 Pub/Sub → 1.0. The repository started from the spec alone (D25).
- Data Cloud, Einstein, CometD and Event Monitoring log files are out of scope (O15).
- Sole maintainer (O14). The risk is reduced with automation: scripted releases, CI, written
  processes and `AGENTS.md`.

## Consequences

- No compatibility shim. A [migration guide](../migrating-from-simple-salesforce.md) replaces it (see ADR 0011).
- API areas not in 0.1 have "planned" pages in the docs, and `sf.request()` covers them until
  they ship.
