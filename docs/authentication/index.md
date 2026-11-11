# Authentication

You give the client one login flow. The client gets the token, keeps it in memory, saves it to
a [token store](token-stores.md) and refreshes it for you.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)

async with esf.AsyncSalesforce(auth) as sf:
    print((await sf.auth.userinfo())["preferred_username"])
```

A login flow is a frozen config object. It holds no tokens and makes no requests.

## Choose a flow

Pick the login flow that matches who your code acts as.

| Login flow | Use it when | Refresh |
|---|---|---|
| [`esf.ClientCredentials`](client-credentials.md) | a backend acts as its own integration user | requests a new token |
| [`esf.JwtBearer`](jwt-bearer.md) | a server acts as a named user, with a certificate | signs a new assertion and requests a new token |
| [`esf.WebServerFlow`](web-server-flow.md) | people log in through the browser and your app acts as each of them | refresh-token grant, safe under rotation |
| [`esf.StaticToken`](static-token.md) | scripts and tests with a token you got elsewhere | none: a `401` raises `SessionExpired` |

Each login flow needs an app in your org. [Salesforce setup](salesforce-setup.md) lists what
each one needs.

## The `domain` argument

Every login flow except `StaticToken` takes `domain=`. It names the host the client logs in to.

| Value | Login host | Use for |
|---|---|---|
| `"login"` | `https://login.salesforce.com` | production and Developer Edition |
| `"test"` | `https://test.salesforce.com` | sandboxes |
| `"https://acme.my.salesforce.com"` | your My Domain | any org; also `https://acme--uat.sandbox.my.salesforce.com` |

```python
print(auth.token_url)  # https://acme.my.salesforce.com/services/oauth2/token
```

If you pass anything else, the login flow raises `ValueError` when you create it. That includes
`http://` URLs, paths and bare host names. `ClientCredentials` needs the My Domain URL, because
Salesforce doesn't support `login` or `test` for that flow.

## Proactive refresh (opt-in)

By default the client refreshes only after Salesforce rejects the token. This is always
correct, because an admin can end sessions at any time. If you know your app's session timeout,
set `refresh_ahead` a little shorter. This saves the rejected request.

```python
from datetime import timedelta

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com",
    client_id="3MVG9...",
    client_secret="...",
    refresh_ahead=timedelta(minutes=110),  # session timeout is 2 h
)
```

You can also pass `expiry="introspect"`. After the client gets a new token, it calls the
introspection endpoint once. It stores the returned `exp` in `Token.expires_at` and refreshes
within 60 seconds of that time. If introspection fails or returns no `exp`, the client refreshes
on `401` instead. It logs this once at INFO on the `esf` logger.

## Token keys

Every login flow has a `token_key`. It names the token's record in the token store.

| Login flow | Default `token_key` |
|---|---|
| `ClientCredentials` | `client_credentials:<hash of domain and client id>` |
| `JwtBearer` | `jwt_bearer:<hash of domain, client id and username>` |
| `WebServerFlow` | none; pass one per Salesforce identity, such as `f"user:{user_id}"` |
| `StaticToken` | `static:<hash of instance URL and token>` |

Clients in one family that use the same key share one in-memory token and one refresh. Events
show only `token_key_hash`, a 12-character SHA-256 prefix of the key.

## The `sf.auth` helpers

`sf.auth` gives you the current token and the OAuth endpoints.

```python
async with esf.AsyncSalesforce(auth) as sf:
    token = await sf.auth.token()
    print(token)  # repr() shows only the last 4 characters of each token
```

| Call | What it does |
|---|---|
| `await sf.auth.token()` | the current `Token`, acquiring one if needed; treat it as a secret |
| `await sf.auth.userinfo()` | `GET /services/oauth2/userinfo` for the current token |
| `await sf.auth.introspect(token_key=None, *, auth=None)` | `POST /services/oauth2/introspect` |
| `await sf.auth.revoke(token_key=None, *, auth=None)` | `POST /services/oauth2/revoke`, then deletes the token from memory and the store |
| `await sf.auth.exchange_code(flow, code, code_verifier=...)` | finishes the [web server flow](web-server-flow.md) |

`Token` is a frozen dataclass with `access_token`, `instance_url`, `issued_at`,
`refresh_token`, `id_url`, `org_id`, `user_id`, `scope`, `signature`, `expires_at` and
`version`. The client parses `user_id` from the identity URL. The token store manages `version`.

## Limitations

- The library doesn't support username and password login or the SOAP `login()` call. Salesforce is retiring
  SOAP `login()` and recommends External Client Apps with OAuth.
- Salesforce's token response has `issued_at` but no `expires_in`, so the client can't know when
  a token expires unless you opt in to [proactive refresh](#proactive-refresh-opt-in).
- `expiry="introspect"` is unverified. Salesforce's introspection docs don't list the response
  fields, so nobody has confirmed that `exp` is always present. This is spec verification item V4.

## How tokens are handled

1. On the first call, the client looks for a token in memory, then in the token store. If it
   finds none, it requests a new one.
2. Each call sends the token the client holds. If Salesforce answers `401` with
   `INVALID_SESSION_ID`, the client refreshes once and resends the call. A second `401` raises
   `SessionExpired`. A `401` without that code can come from an Apex REST class that already
   ran. The client resends such a call only if it is a read.
3. The client checks the `instance_url` of every token it gets from the token endpoint or the
   token store. It must be `https://`, or `http://` to a loopback host. Otherwise the client
   raises an error and doesn't send the bearer token.
4. Concurrent calls share one refresh. If many tasks or threads hit an expired token at once,
   the client sends exactly one token request. The rest wait for it. If it fails, every waiting
   caller gets the same error. The next call tries again.
5. For `WebServerFlow`, the client saves a rotated refresh token to the store before it uses the
   new access token. It never resends a refresh request that may have reached Salesforce,
   because resending a rotated-out refresh token makes Salesforce revoke the whole token chain.
   If such a request times out, the client re-reads the store. If no other process stored a new
   token, it raises `RefreshFailed(ambiguous=True)`.
6. If two requests use the same refresh token at once, Salesforce answers "token request is
   already being processed". The client waits, re-reads the store and tries again, with at most 3 token
   requests in total.
7. If the refresh token is dead, Salesforce answers `invalid_grant`. The client raises
   `ReauthorizationRequired` and emits the `sf.auth.reauth_required` event. The user has to log
   in again.
8. If a `WebServerFlow` has no stored refresh token, the client raises `ReauthorizationRequired`
   with `error_code="no_refresh_token"`. It sends no token request.
9. For `ClientCredentials` and `JwtBearer`, `invalid_grant` means a configuration problem and
   raises `OAuthError`.
10. A new token must belong to the same org and user as the token it replaces. This applies to
    a refreshed token and to a token the client re-reads from the token store. If the org id or
    user id differs, the client raises `TokenIdentityChanged` and doesn't use the token.

Token requests to `/services/oauth2/token` don't count as API calls in `sf.request` events.
They emit `sf.auth.refreshed` instead. See [Logging](../logging.md).
