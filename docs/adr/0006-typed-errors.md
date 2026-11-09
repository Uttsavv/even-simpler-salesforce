# ADR 0006: Typed errors with one shape; opt-in tags; no client-side validation

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions D11, D12, D13 (details in §8)

## Context

Errors in the older libraries came as a few broad classes whose content callers had to
inspect. Partial failures and HTML proxy pages were easy to mistake for success, or crashed the
error handling itself.

## Decision

- **Every** error is an `esf.SalesforceError` with one set of attributes (`error_code`, `fields`,
  `status`, `operation`, `request_id`, `errors`, ...), whatever the source: REST, OAuth,
  Collections, and later Bulk and SOAP (D11). Specific classes are chosen by error code, then
  by HTTP status.
- Custom errors are **opt-in by tag**: a `[TAG]` prefix in a validation-rule message maps to a
  user class that subclasses the default class (D12).
- The client does **not** evaluate validation rules or trigger logic (D13). An opt-in
  describe-based pre-check may come after 1.0.

## Consequences

- Exceptions can be pickled, and building them never fails on odd bodies (guard tests).
- See [Errors](../errors.md).
