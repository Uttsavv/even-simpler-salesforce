# ADR 0007: Plain dicts, opaque cursors, cached describes

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions O6, O9, O12 (details in §9.1–§9.3, §12)

## Context

Results need to be easy to use without a schema, and safe to pass to a browser. Describe calls
are expensive and are needed again and again.

## Decision

- Records are **plain `dict`s** by default. `model=` accepts Pydantic models or other callables
  (O6).
- Query pagination uses an **opaque `Cursor`**: the base64url `nextRecordsUrl`, validated against
  the exact locator path shape, and optionally HMAC-signed with an app key (O9).
- Describes go through a **cache** (in-memory TTL/LRU by default, or any `Cache` implementation)
  keyed by org, API version, sObject and user hash, and are revalidated with `If-Modified-Since`
  (O12).

## Consequences

- A cursor sent by a client can't make the server call other URLs.
- Describe results can be shared across processes through a user-supplied cache.
