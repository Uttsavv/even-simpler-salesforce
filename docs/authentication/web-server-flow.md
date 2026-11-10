# Web server flow

People log in to Salesforce through the browser, and your app calls the API as each of them.
The flow uses PKCE and keeps each user's refresh token in a [token store](token-stores.md).

```python
import even_simpler_salesforce as esf


def flow_for(user_id: str) -> esf.WebServerFlow:
    return esf.WebServerFlow(
        domain="https://acme.my.salesforce.com",  # or "login", "test" or a site URL
        client_id="3MVG9...",
        client_secret="...",  # optional for public clients (PKCE only)
        redirect_uri="https://app.example.com/oauth/callback",
        token_key=f"user:{user_id}",  # one key per Salesforce identity
    )
```

`token_key` is required. It names the token-store record for this identity. Without it, every
user of a multi-user app would share one token record.

A login takes three steps: redirect the user, handle the callback, then call the API as the
user. You need an app with a matching callback URL; [Salesforce setup](salesforce-setup.md#web-server-flow)
shows how.

## Redirect the user

`authorization_url()` builds the login URL. It makes no request. It returns an
`AuthStart(url, state, code_verifier)`. Keep `state` and `code_verifier` in the user's
server-side session until the callback. `repr()` of an `AuthStart` leaves both out.

```python
flow = flow_for("42")
start = flow.authorization_url()
session = {"oauth_state": start.state, "pkce_verifier": start.code_verifier}
print(start.url)  # send the browser here
```

| Argument | Sent as |
|---|---|
| `state` | `state`; a random 32-character token by default |
| `scopes` | `scope`, overriding the flow's scopes for this login |
| `prompt` | `prompt`, such as `"login"` or `"consent"` |
| `login_hint` | `login_hint`, the username to prefill |

## Handle the callback

Salesforce redirects the browser to `redirect_uri?code=...&state=...`. Check `state` against the
session, then exchange the code. `exchange_code` stores the tokens under `flow.token_key` and
returns the `Token`.

!!! warning "Use a durable, shared token store in production"
    If you keep the default in-memory token store, it loses refresh tokens when the process
    restarts. Other processes can't see them either. With refresh-token rotation on, two processes with
    different copies of a user's refresh token make Salesforce revoke it. Pass a
    [durable token store](token-stores.md) with compare-and-swap:
    `esf.AsyncSalesforce(auth, store=MyStore(...))`.

```python
callback_params = {"code": "aPrx...", "state": start.state}  # from the callback request

assert callback_params["state"] == session["oauth_state"]

sf = esf.AsyncSalesforce(esf.StaticToken("unused", "https://acme.my.salesforce.com"))
token = await sf.auth.exchange_code(flow, callback_params["code"], code_verifier=session["pkce_verifier"])
print(token.user_id, token.org_id)
```

If Salesforce rejects the code, `exchange_code` raises `OAuthError` with Salesforce's `error` and
`error_description`. This happens if the code is already used or expired, or the verifier is
wrong.

The root client here uses a placeholder `StaticToken`, because it never makes calls itself. In a
real app the root is often your integration-user client, with `ClientCredentials` or
`JwtBearer`.

## Call the API as the user

Create a child client for the user's flow with `with_auth()`:

```python
as_user = sf.with_auth(flow_for("42"))
page = await as_user.query("SELECT Id, Name FROM Account LIMIT 5")
print([a["Name"] for a in page])
```

A child shares the root client's connection pool, caches and observers. It uses the root's token
store unless you pass `store=`. It doesn't inherit the root's `instance_url=` override, so pass
`instance_url=` to `with_auth()` if the child needs one.

## Ask the user to log in again

When the user's refresh token is expired, revoked or rotated out, the call raises
`ReauthorizationRequired`. Send the user through the redirect step again.

```python
try:
    await as_user.query("SELECT Id FROM Account LIMIT 1")
except esf.ReauthorizationRequired:
    start = flow_for("42").authorization_url()  # redirect the user here
```

## Log out

`revoke()` revokes the refresh token, or the access token when there is no refresh token. It
then deletes the record from memory and from the store.

```python
await sf.auth.revoke(auth=flow_for("42"))
await sf.aclose()
```

To check whether a stored token is still active, call `sf.auth.introspect(token_key, auth=flow)`.

## Arguments

| Argument | Default | What it does |
|---|---|---|
| `domain` | required | `"login"`, `"test"`, a My Domain URL or an [Experience Cloud site URL](index.md#experience-cloud-sites) |
| `client_id` | required | consumer key of the app |
| `redirect_uri` | required | must exactly match the app's callback URL |
| `token_key` | required | [token store](token-stores.md) key, one per Salesforce identity |
| `client_secret` | `None` | consumer secret; public clients can leave it out and rely on PKCE |
| `scopes` | `("api", "refresh_token")` | scopes requested at login |
| `pkce` | `True` | S256 PKCE; `False` turns it off for apps that can't use it |
| `refresh_ahead` | `None` | [refresh proactively](index.md#proactive-refresh-opt-in) after this `timedelta` |
| `expiry` | `"unknown"` | `"introspect"` reads the expiry from the introspection endpoint |

## How refresh works

When the access token expires, the client refreshes it with the stored refresh token.

- Only one refresh runs at a time per `token_key` within a client family. The store's `lock()`
  extends this to other client families. The built-in in-memory stores lock per process, and a
  durable store's `lock()` can extend it across processes.
- The client saves a rotated refresh token to the store with compare-and-swap. It does this
  before it uses the new access token. If another process saved first, the client uses that process's token.
- The client never resends a refresh request that timed out or was reset after it was sent. It
  waits briefly and re-reads the store. If no other process stored a new token, it raises
  `RefreshFailed(ambiguous=True)`.
- If the refresh token is expired, revoked or rotated out, Salesforce answers `invalid_grant`.
  The client raises `ReauthorizationRequired` and emits `sf.auth.reauth_required`.
- If the store has no refresh token under `token_key`, the first call raises
  `ReauthorizationRequired` with `error_code="no_refresh_token"`. The client doesn't contact
  Salesforce and emits no event. This happens if the user never finished the callback step or
  logged out.
