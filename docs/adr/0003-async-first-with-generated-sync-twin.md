# ADR 0003: Async first, with a generated sync twin

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions D6, O2, O5 (details in §2, §5.4)

## Context

Modern Python services are increasingly async, but many users (scripts, Flask, Django,
Celery) are sync. Maintaining two hand-written clients would let them drift apart.

## Decision

- `AsyncSalesforce` is written by hand in `_async/`, and it is the only code that awaits (D6).
- `Salesforce` is **generated** from it by `scripts/unasync.py` into `_sync/`. CI fails if the
  generated code is stale (O5). Only the small `_compat.py` primitive modules are written by
  hand for both sides.
- Pure logic (`core/`, config classes, `errors.py`, `models.py`) does no I/O and is shared
  unchanged.
- The lowest supported Python is 3.10; CI tests 3.10–3.14 (O2).

## Consequences

- Both clients have the same methods, arguments and behaviour, and every contract test runs
  against both.
- The sync client uses threads where the async one uses tasks (Collections chunks, the limits
  poller).
