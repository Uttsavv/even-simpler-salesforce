# ADR 0005: Login flows as config objects; rotation-safe refresh

- **Status:** accepted, 2026-11-09
- **Source:** [`big-bang.md`](https://github.com/Uttsavv/even-simpler-salesforce/blob/main/big-bang.md) §2, decisions D8 (auth), D9, D10, O11 (details in §6)

## Context

Salesforce token responses have no `expires_in`. Refresh-token rotation invalidates the previous
token, and reusing a rotated-out token revokes the chain. Concurrent refreshes with one refresh
token fail with "token request is already being processed". SOAP `login()` is being retired.

## Decision

- Each login flow is a frozen `Auth` config object that holds no tokens: `WebServerFlow` (PKCE),
  `ClientCredentials`, `JwtBearer` and `StaticToken` (D9). Long-lived tokens go through a
  pluggable `TokenStore` (D8).
- No SOAP `login()`, no zeep, no attribute magic (D10).
- The access token is kept in memory. On `401 INVALID_SESSION_ID` the client refreshes **once**
  and resends. Concurrent callers share that one refresh.
- A rotated refresh token is saved before the new access token is used. A refresh request that
  may have reached Salesforce is never resent. "Already being processed" is handled by waiting
  and re-reading the store. Proactive refresh is opt-in.
- Multi-process safety: stores may provide `lock(key)` and must make `save(expected_version=)` a
  compare-and-swap (O11). In-process single-flight always applies.

## Consequences

- `WebServerFlow` requires an explicit `token_key` (one per Salesforce identity).
- A reusable `TokenStoreContract` suite lets apps check their own stores. See
  [Token stores](../authentication/token-stores.md).
