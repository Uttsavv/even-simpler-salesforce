# Authentication

A client gets one **login flow** object. Flows are frozen config objects. They hold no tokens
and make no requests. The client gets the token, keeps it in memory, saves it to a
[token store](token-stores.md) and refreshes it.

| Flow | Use it when | Refresh | Page |
|---|---|---|---|
| `esf.ClientCredentials` | a backend acts as its own integration user | requests a new token | [Client credentials](client-credentials.md) |
| `esf.JwtBearer` | server-to-server with a certificate, acting as a named user | signs a new assertion and requests a new token | [JWT bearer](jwt-bearer.md) |
| `esf.WebServerFlow` | people log in through the browser and your app acts as each of them | refresh-token grant, safe under rotation | [Web server flow (PKCE)](web-server-flow.md) |
| `esf.StaticToken` | scripts and tests with a token you got elsewhere | none: a `401` raises `SessionExpired` | [Static token](static-token.md) |

Username/password login and the SOAP `login()` call are **not supported**. Salesforce is
retiring SOAP `login()` and recommends External Client Apps with OAuth.

## The `domain` argument

Each flow except `StaticToken` takes `domain=`:

- `"login"`: `https://login.salesforce.com` (production and Developer Edition);
- `"test"`: `https://test.salesforce.com` (sandboxes);
- a full origin such as `"https://acme.my.salesforce.com"` or
  `"https://acme--uat.sandbox.my.salesforce.com"` (your My Domain).

Anything else (`http://`, a path, a bare host name) raises `ValueError` when the flow is
created. `ClientCredentials` **requires** the My Domain URL, because Salesforce doesn't support
`login` / `test` for that flow.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
print(auth.token_url)
```

## How tokens are handled

1. **First call.** The client looks the token up in memory, then in the token store, and
   otherwise requests a new one. Salesforce's token response has `issued_at` but no
   `expires_in`, so the client doesn't know when the token expires.
2. **Each call** sends the token it holds. When Salesforce answers `401` (`INVALID_SESSION_ID`),
   the client refreshes **once** and resends the call. A second `401` raises `SessionExpired`.
3. **Concurrent calls share one refresh.** When many tasks or threads hit an expired token at
   the same time, exactly one token request is made and the rest wait for it. If that request
   fails, the callers that were waiting all get the same error instead of each sending another
   token request. A call made after the failure tries again.
4. **Rotation-safe.** For `WebServerFlow`, a rotated refresh token is saved to the store
   **before** the new access token is used. The refresh request is **never resent** once it
   may have reached Salesforce, because resending a rotated-out refresh token makes Salesforce
   revoke the whole token chain. If such a request times out, the client re-reads the store,
   and raises `RefreshFailed(ambiguous=True)` if no other process stored a new token.
5. **"Token request is already being processed."** Salesforce returns this when the same
   refresh token is used concurrently. The client waits, re-reads the store and tries again,
   with at most 3 token requests in total.
6. A dead refresh token (`invalid_grant`) raises `ReauthorizationRequired` and emits the
   `sf.auth.reauth_required` event: the user has to log in again. For `ClientCredentials` and
   `JwtBearer`, `invalid_grant` means a configuration problem and raises `OAuthError`.

Token requests to `/services/oauth2/token` are not counted as API calls in `sf.request`
events. They emit `sf.auth.refreshed` instead (see [Logging](../logging.md)).

## Proactive refresh (opt-in)

By default the client refreshes only after Salesforce rejects the token. This is always
correct, because an admin can end sessions at any time. If you know your app's session timeout,
you can refresh ahead of it to save the rejected request:

```python
from datetime import timedelta

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com",
    client_id="3MVG9...",
    client_secret="...",
    refresh_ahead=timedelta(minutes=110),  # session timeout is 2 h
)
```

`expiry="introspect"` is another option. After each token is acquired, the client calls the
introspection endpoint once and stores the returned `exp` in `Token.expires_at`. It then
refreshes within 60 seconds of that time. If introspection fails or returns no `exp`, the client
falls back to 401-driven refresh and logs this once at INFO on the `esf` logger.

!!! warning "Unverified"
    Salesforce's introspection documentation doesn't list the response fields, so it is not yet
    confirmed that `exp` is always present (verification item V4 in the spec).

## Token keys

Every flow has a `token_key`, which names the record in the token store:

| Flow | Default `token_key` |
|---|---|
| `ClientCredentials` | `client_credentials:<hash of domain and client id>` |
| `JwtBearer` | `jwt_bearer:<hash of domain, client id and username>` |
| `WebServerFlow` | **required**; one per Salesforce identity, e.g. `f"user:{user_id}"` |
| `StaticToken` | `static:<hash of instance URL and token>` |

Clients in one family that use the same key share one in-memory token and one refresh. Only a
12-character SHA-256 prefix of the key (`token_key_hash`) ever appears in events.

## The `sf.auth` helpers

| Call | Does |
|---|---|
| `await sf.auth.token()` | the current `Token`, acquiring one if needed (treat it as a secret) |
| `await sf.auth.userinfo()` | `GET /services/oauth2/userinfo` for the current token |
| `await sf.auth.introspect(token_key=None, *, auth=None)` | `POST /services/oauth2/introspect` |
| `await sf.auth.revoke(token_key=None, *, auth=None)` | `POST /services/oauth2/revoke`, then deletes the token from memory and the store |
| `await sf.auth.exchange_code(flow, code, code_verifier=...)` | finishes the [web server flow](web-server-flow.md) |

```python
async with esf.AsyncSalesforce(auth) as sf:
    token = await sf.auth.token()
    print(token)  # repr() shows only the last 4 characters of each token
    print((await sf.auth.userinfo())["preferred_username"])
```

`Token` is a frozen dataclass with `access_token`, `instance_url`, `issued_at`,
`refresh_token`, `id_url`, `org_id`, `user_id` (parsed from the identity URL), `scope`,
`signature`, `expires_at` and the store-managed `version`.
