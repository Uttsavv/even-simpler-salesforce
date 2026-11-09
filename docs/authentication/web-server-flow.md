# Web server flow (PKCE)

Use this flow when people log in to Salesforce through the browser and your app calls the API
**as each of them**. Your app keeps each user's refresh token in a
[token store](token-stores.md).

```python
import even_simpler_salesforce as esf


def flow_for(user_id: str) -> esf.WebServerFlow:
    return esf.WebServerFlow(
        domain="https://acme.my.salesforce.com",  # or "login" / "test"
        client_id="3MVG9...",
        client_secret="...",  # optional for public clients (PKCE only)
        redirect_uri="https://app.example.com/oauth/callback",
        scopes=("api", "refresh_token"),  # the default
        token_key=f"user:{user_id}",  # required: one key per Salesforce identity
    )
```

`token_key` is required. It names the token-store record that holds this identity's tokens.
Without it, every user of a multi-user app would share one token record. PKCE (S256) is on by
default. `pkce=False` turns it off for apps that can't use it.

## 1. Redirect the user

`authorization_url()` is pure: it builds the URL and makes no request. It returns an
`AuthStart(url, state, code_verifier)`. Keep `state` and `code_verifier` in the user's
server-side session until the callback.

```python
flow = flow_for("42")
start = flow.authorization_url()  # random state; or authorization_url(state=..., prompt="login")
session = {"oauth_state": start.state, "pkce_verifier": start.code_verifier}
print(start.url)  # send the browser here
```

| Argument | Sent as |
|---|---|
| `state=` | `state` (a random 32-character token by default) |
| `scopes=` | `scope`, overriding the flow's scopes for this login |
| `prompt=` | `prompt`, e.g. `"login"` or `"consent"` |
| `login_hint=` | `login_hint`, the username to prefill |

## 2. Handle the callback

Salesforce redirects to `redirect_uri?code=...&state=...`. Check `state` against the session,
then exchange the code. `exchange_code` stores the tokens under `flow.token_key` and returns
the `Token`:

```python
callback_params = {"code": "aPrx...", "state": start.state}  # from the callback request

assert callback_params["state"] == session["oauth_state"]

sf = esf.AsyncSalesforce(esf.StaticToken("unused", "https://acme.my.salesforce.com"))
token = await sf.auth.exchange_code(flow, callback_params["code"], code_verifier=session["pkce_verifier"])
print(token.user_id, token.org_id)
```

A rejected code (already used, expired, wrong verifier) raises `OAuthError` with Salesforce's
`error` / `error_description`.

## 3. Call the API as the user

Create a child client for the user's flow. Children share the root client's connection pool,
caches and observers. They use the root's token store unless you pass `store=`:

```python
as_user = sf.with_auth(flow_for("42"))
page = await as_user.query("SELECT Id, Name FROM Account LIMIT 5")
print([a["Name"] for a in page])
```

The root client in this example uses a placeholder `StaticToken` because it never makes calls
itself. In a real app the root is often your integration-user client
(`ClientCredentials` or `JwtBearer`), and per-user children come from `with_auth()`.

!!! warning "Use a durable, shared store in production"
    The default in-memory store loses refresh tokens on restart, and other processes can't see
    it. With refresh-token rotation on, two processes holding different copies of a user's
    refresh token will eventually make Salesforce revoke it. Pass a
    [durable store](token-stores.md) with compare-and-swap:
    `esf.AsyncSalesforce(auth, store=MyStore(...))`.

## Refresh and rotation

When the access token expires, the client refreshes it with the stored refresh token:

- Only one refresh runs at a time per `token_key` within a client family. The store's `lock()`
  extends this to other client families (the built-in in-memory stores lock per process), and a
  durable store's `lock()` extends it across processes.
- A rotated refresh token is saved to the store (compare-and-swap) **before** the new access
  token is used. If another process saved first, the client uses that process's token.
- A refresh request that timed out or was reset after sending is **not resent**. The client
  waits briefly and re-reads the store. If no other process stored a new token, it raises
  `RefreshFailed(ambiguous=True)`.
- `invalid_grant` (refresh token expired, revoked or rotated out) raises
  `ReauthorizationRequired` and emits `sf.auth.reauth_required`. Send the user through step 1
  again.

```python
try:
    await as_user.query("SELECT Id FROM Account LIMIT 1")
except esf.ReauthorizationRequired:
    start = flow_for("42").authorization_url()  # ask the user to log in again
```

## Log out

`revoke()` revokes the refresh token (or the access token, when there is no refresh token) and
deletes the record from memory and from the store:

```python
await sf.auth.revoke(auth=flow_for("42"))
await sf.aclose()
```

`sf.auth.introspect(token_key, auth=flow)` asks Salesforce whether a stored token is still
active.
