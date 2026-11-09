# ADR 0004: One request pipeline on httpx; core depends on httpx only

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions D7, D8 (transport), D16, O7, O10 (details in §5, §7, §12)

## Context

In the older libraries, some API areas built their own requests and so skipped refresh,
timeouts, proxies or logging. Heavy dependencies also made installs fragile.

## Decision

- **httpx** is the only required dependency, with one connection pool per client family (D7,
  O7). pydantic, orjson, h2, PyJWT/cryptography, OpenTelemetry, structlog, FastAPI and Flask are
  optional extras.
- Every API call goes through **one transport pipeline**: auth → budget guard → concurrency
  gate → compression → send → usage header → classify → refresh/retry → one event. The HTTP
  client can be injected (D8). An AST test makes sure that only the transport sends requests and
  that no API version is hard-coded.
- Performance comes from token reuse, connection reuse, gzip, usage from headers, a describe
  cache, fewer round trips and `Retry-After`-aware backoff (D16).
- Write retries (O10) were first "retry PATCH/PUT/DELETE on 5xx and timeouts". **This is
  superseded by [ADR 0001](0001-retry-safety.md):** only reads are resent after a request may
  have reached Salesforce.

## Consequences

- A new API area automatically gets refresh, retries, typed errors, usage tracking and logging.
- `sf.request()` is the same pipeline exposed for any REST path.
