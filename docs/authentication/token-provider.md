# Token provider

You write a function that returns a token, and the client calls it when it needs one. Use it to
reuse a login you already have, such as the Salesforce CLI, a secrets manager or your platform.

```python
import os
from datetime import datetime, timezone

import even_simpler_salesforce as esf


def get_token() -> esf.Token:
    return esf.Token(
        access_token=os.environ["SF_ACCESS_TOKEN"],  # or read it from your secrets manager
        instance_url=os.environ["SF_INSTANCE_URL"],
        issued_at=datetime.now(timezone.utc),
    )


auth = esf.TokenProvider(get_token)

async with esf.AsyncSalesforce(auth) as sf:
    print((await sf.limits())["DailyApiRequests"].remaining)
```

The client calls `get_token` for the first request. If Salesforce rejects the token with `401`,
the client calls it once more and resends the request. If Salesforce rejects the new token too,
the call raises `SessionExpired`.

## Arguments

| Argument | Default | What it does |
|---|---|---|
| `get_token` | required | a function with no arguments that returns an `esf.Token` |
| `token_key` | unique to this `TokenProvider` | [token store](token-stores.md) key |

`get_token` must return a `Token` with `access_token`, `instance_url` and `issued_at`. The other
fields are optional. `repr(auth)` doesn't show the function.

## Reuse the Salesforce CLI login

`sf org display --json` prints the access token and the instance URL of an org you logged in to
with the Salesforce CLI. You need the `sf` CLI installed and logged in to the org.

```python
import json
import subprocess


def sf_cli_token() -> esf.Token:
    out = subprocess.run(
        ["sf", "org", "display", "--json", "--target-org", "my-sandbox"],
        capture_output=True,
        check=True,
        text=True,
    )
    result = json.loads(out.stdout)["result"]
    return esf.Token(
        access_token=result["accessToken"],
        instance_url=result["instanceUrl"],
        issued_at=datetime.now(timezone.utc),
    )


auth = esf.TokenProvider(sf_cli_token)
```

Pass `auth` to the client as usual. The client runs `sf` only when it needs a token. Use this
for local scripts and tools. For services, use a login flow that doesn't depend on a CLI, such
as [client credentials](client-credentials.md).

## Async and sync clients

`get_token` is a plain function, so one `TokenProvider` works with both clients.

- `AsyncSalesforce` runs `get_token` in a worker thread. A blocking call such as
  `subprocess.run` doesn't block the event loop.
- `Salesforce` calls `get_token` in the thread that sends the request.

## Errors

If `get_token` raises, the call raises `RefreshFailed`. The message names the exception type but
not its message. The original exception is `__cause__`, and logged tracebacks print it, so don't
put credentials in your exception messages. The next call tries `get_token` again.

`get_token` must be a plain function. An `async def` function raises `TypeError` when you create
the `TokenProvider`.

```python
def logged_out() -> esf.Token:
    raise esf.ReauthorizationRequired("run: sf org login web --alias my-sandbox")
```

If `get_token` raises an `esf.SalesforceError`, such as `ReauthorizationRequired`, the client
raises it unchanged. Use this to tell the caller what to do.

If `get_token` returns something other than a `Token`, or an `instance_url` that isn't
`https://`, the call raises `RefreshFailed`. The client doesn't send the token.

## Share the token between processes

The client saves each token from `get_token` to the [token store](token-stores.md). To share
one token across processes, pass a durable token store and the same `token_key` in each process.
A process then uses the stored token and calls `get_token` only after a `401`.

```python
store = esf.AsyncInMemoryTokenStore()  # use a durable store here
auth = esf.TokenProvider(get_token, token_key="cli:my-sandbox")

async with esf.AsyncSalesforce(auth, store=store) as sf:
    await sf.limits()
```

The default `token_key` is unique to each `TokenProvider` object, so two objects never share a
record. With a durable store, always pass `token_key`. Otherwise each process run adds a record.

## Refresh before the token expires

If you know when the token expires, set `expires_at` on the `Token`. The client then calls
`get_token` within 60 seconds of that time, before Salesforce rejects the token.

## Revoke and introspect

These work as for [`StaticToken`](static-token.md#revoke-and-introspect).

- `sf.auth.revoke()` revokes the current token through the instance's revoke endpoint.
- `sf.auth.introspect()` needs an OAuth client, which a token provider doesn't have. Pass
  `auth=` with the login flow that issued the token.

## How it works

- Concurrent calls share one call to `get_token`. If many tasks or threads get `401` at once,
  the client calls `get_token` once and the rest wait for its token.
- If `get_token` fails, every caller that waited gets the same error.
- The client checks that a new token belongs to the same org and user as the token it
  replaces, if both tokens carry those ids. Otherwise it raises `TokenIdentityChanged`.
- Each call to `get_token` emits `sf.auth.refreshed` with `flow="token_provider"`. Events never
  contain the token.
