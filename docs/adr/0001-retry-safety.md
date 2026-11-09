# ADR 0001: Only reads are resent after a request may have reached Salesforce

- **Status:** accepted, 2026-11-09
- **Supersedes:** the "PATCH / PUT / DELETE" column of the retry table in `big-bang.md` §7.4, and
  "upsert by external id" in its list of naturally idempotent specs.

## Context

The first 0.1 implementation treated every method except POST as safe to resend after a timeout,
a dropped connection or a 5xx. A side review (comparing with lwc-soql-builder / sfbench) showed
that this is wrong for Salesforce:

- `PATCH sobjects/{o}/{id}`: the first request may have committed; the resend runs triggers,
  flows and outbound emails a second time. With `If-Unmodified-Since` the resend gets a false
  `412 Conflict`.
- `DELETE sobjects/{o}/{id}`: the first delete succeeded; the resend raises a false `NotFound` /
  `ENTITY_IS_DELETED`.
- Upsert by external id: a resend after a create reports `created=False` and runs triggers again.
- Some GETs run code: Apex REST (`/services/apexrest/...`) and Tooling `executeAnonymous`.

"Idempotent" in HTTP terms is about the resource state, not about side effects in the org.

## Decision

A request is resent after it **may have reached Salesforce** (timeout or reset after sending,
or a retryable 5xx) only if it is a read:

- `GET` / `HEAD`, **except** Apex REST paths and `tooling/executeAnonymous`, and
- not marked `dml=True`,
- unless the spec (or `sf.request(..., idempotent=...)`) says otherwise explicitly.

Reads that use POST (query by POST, `parameterizedSearch`, Collections retrieve) keep an
explicit `idempotent=True`.

Unchanged: requests that **never left the client** (connect errors) are always resent, and so are
requests Salesforce explicitly **rejected without running** (`UNABLE_TO_LOCK_ROW`, 429 /
`REQUEST_LIMIT_EXCEEDED` with `Retry-After`), for every method.

## Consequences

- A timeout on `update`/`delete`/`upsert`/Collections writes now surfaces as `RequestTimeout` (or
  `ServerError`) to the caller, who knows whether a second run is acceptable.
- Callers that know a write is safe to repeat (no triggers, deterministic) can opt in with
  `sf.request(..., idempotent=True)`.
- Guarded by `tests/unit/test_retry.py` and `tests/contract/test_review_fixes.py` (both clients).
