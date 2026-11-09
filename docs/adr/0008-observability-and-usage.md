# ADR 0008: Canonical logs and free usage tracking

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions D15, D27 (details in §10, §11)

## Context

Debugging Salesforce integrations needs one complete record per call, plus a roll-up per unit
of work. API limits are shared by the whole org, so usage must be visible without spending more
calls.

## Decision

- **One wide `sf.request` event per logical call**, emitted after retries and refreshes, plus a
  `capture()` roll-up to merge into the app's own log line. Secrets are always redacted.
  `ESF_DEBUG=1` prints readable lines (D15). The library never configures logging; observers
  adapt events to stdlib logging, structlog or OpenTelemetry.
- **Usage** comes from `Sforce-Limit-Info` on every response, with counters by operation, object
  and caller, `sf.usage.snapshot()`, threshold events and an optional guard that pauses
  low-priority callers. `/limits` polling is opt-in (D27).

## Consequences

- Error messages are added to events only with `ESF_DEBUG`, because they can quote record values.
- See [Logging](../logging.md) and [Usage & budgets](../usage-and-budgets.md).
