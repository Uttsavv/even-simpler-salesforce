# Getting started

## Requirements

- Python 3.10 or newer.
- A Salesforce org with an **External Client App** (or a classic Connected App) that your code
  can log in through. [Salesforce setup](authentication/salesforce-setup.md) walks through it.
- API version **59.0 or newer**. The library defaults to **68.0**, which is pinned for each
  library release and never set to "latest".

## Install

```sh
pip install even-simpler-salesforce
# optional extras, e.g. JWT bearer login and orjson:
pip install "even-simpler-salesforce[jwt,speed]"
```

| Extra | Installs | Enables |
|---|---|---|
| `jwt` | PyJWT, cryptography | [`JwtBearer`](authentication/jwt-bearer.md) |
| `pydantic` | Pydantic 2 | `model=` with Pydantic models |
| `speed` | orjson | faster JSON (same behaviour as the stdlib codec) |
| `http2` | `httpx[http2]` | `http2=True` |
| `otel` | opentelemetry-api | the OpenTelemetry observer |
| `structlog` | structlog | the structlog observer |
| `testing` | pytest, respx | `even_simpler_salesforce.testing` helpers that need them |
| `fastapi`, `flask` | the framework | the example apps |

The import name is `even_simpler_salesforce`. These docs alias it as `esf`:

```python
import even_simpler_salesforce as esf

print(esf.__version__)
```

## Your first call

The examples read credentials from environment variables. For the
[client-credentials flow](authentication/client-credentials.md) you need the org's **My Domain**
URL and the app's consumer key and secret:

```sh
export SF_DOMAIN=https://acme.my.salesforce.com
export SF_CLIENT_ID=3MVG9...
export SF_CLIENT_SECRET=...
```

=== "Async"

    ```python
    import asyncio
    import os

    import even_simpler_salesforce as esf


    async def main() -> None:
        auth = esf.ClientCredentials(
            domain=os.environ["SF_DOMAIN"],
            client_id=os.environ["SF_CLIENT_ID"],
            client_secret=os.environ["SF_CLIENT_SECRET"],
        )
        async with esf.AsyncSalesforce(auth) as sf:
            print(await sf.auth.userinfo())
            print(await sf.count("SELECT COUNT() FROM Lead"))


    asyncio.run(main())
    ```

=== "Sync"

    ```python
    import os

    import even_simpler_salesforce as esf

    auth = esf.ClientCredentials(
        domain=os.environ["SF_DOMAIN"],
        client_id=os.environ["SF_CLIENT_ID"],
        client_secret=os.environ["SF_CLIENT_SECRET"],
    )
    with esf.Salesforce(auth) as sf:
        print(sf.auth.userinfo())
        print(sf.count("SELECT COUNT() FROM Lead"))
    ```

Nothing is sent when the client is created. The first call fetches a token and keeps it in
memory. Later calls reuse it until Salesforce rejects it, and then the client refreshes once
and resends the call.

!!! tip "Top-level `await` in these docs"
    Most async examples use `await` at the top level to keep them short, as you would in
    `python -m asyncio` or a notebook. In a script, put them inside an `async def` and call it
    with `asyncio.run()`, as above. Every async example has a sync version: drop `async` and
    `await`, and use `with esf.Salesforce(...)`.

## Async and sync

`AsyncSalesforce` is written by hand. `Salesforce` is generated from it, so both have the same
methods, arguments and behaviour. Pick one per program:

| | `AsyncSalesforce` | `Salesforce` |
|---|---|---|
| Use with | asyncio apps (FastAPI, aiohttp, workers) | scripts, Flask, Django, Celery |
| Context manager | `async with` | `with` |
| Close | `await sf.aclose()` | `sf.close()` |
| `query_iter` | async iterator (`async for`) | iterator (`for`) |
| Thread/task safety | share one client across tasks | share one client across threads |

Create **one client per process** and share it. Each client owns an httpx connection pool. Use
the [children](#child-clients) below to vary auth or tags.

## Records, queries and collections in one minute

```python
auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

leads = sf.sobject("Lead")
lead_id = await leads.create({"FirstName": "Ada", "LastName": "Lovelace", "Company": "Engines"})
await leads.update(lead_id, {"Status": "Working"})
lead = await leads.get(lead_id, fields=["Id", "Name", "Status"])

page = await sf.query(esf.soql("SELECT Id, Name FROM Lead WHERE Status = {}", "Working"))
print(page.total_size, [r["Name"] for r in page])

result = await sf.collections.update("Lead", [{"Id": lead_id, "Rating": "Hot"}])
result.raise_for_errors()

await sf.aclose()
```

## Configuring the client

Every option is a keyword argument, and none of them is required:

```python
sf = esf.AsyncSalesforce(
    auth,
    api_version="68.0",                  # 59.0 or newer
    timeout=esf.Timeouts(connect=5, read=60, write=60, pool=5),
    retry=esf.RetryPolicy(max_attempts=3),
    max_concurrency=20,                  # in-flight requests for the whole client family
    client_name="acme-sync",             # Sforce-Call-Options: client=acme-sync
    budget=esf.Budget(warn_at=(0.5, 0.8, 0.95)),
)
await sf.aclose()
```

| Argument | Default | What it does |
|---|---|---|
| `api_version` | `"68.0"` | API version for every URL; must look like `"NN.N"` and be ≥ 59.0 |
| `http_client` | owned client | inject your own `httpx.AsyncClient` / `httpx.Client` (the library never closes it) |
| `store` | in-memory | [token store](authentication/token-stores.md) |
| `cache` | `MemoryCache(500)` | describe cache (any object with `get` / `set` / `delete`) |
| `describe_cache` | `DescribeCacheConfig()` | TTL (1 h), revalidation, size |
| `observers` | `()` | [event observers](logging.md) |
| `budget` | `None` | [usage thresholds and guard](usage-and-budgets.md) |
| `retry` | `RetryPolicy()` | [retry policy](errors.md#retries) |
| `timeout` | `Timeouts()` | 5 s connect / 60 s read / 60 s write / 5 s pool; ignored with `http_client` |
| `max_concurrency` | unlimited | semaphore shared by the client family |
| `user_agent` | `even-simpler-salesforce/<version>` | `User-Agent` header |
| `json` | orjson or stdlib | object with `dumps(obj) -> bytes` and `loads(bytes)` |
| `error_tags` | `{}` | [custom error classes](errors.md#custom-errors-with-tags) |
| `cursor_key` | `None` | HMAC key for [signed cursors](guides/query.md#cursors) |
| `instance_url` | from the token | override the instance URL |
| `log_soql` | `"hash"` | `"redacted"` also logs SOQL with literals replaced by `?` |
| `http2` | `False` | HTTP/2 on the owned client (`[http2]` extra) |
| `client_name` | `None` | sent as `Sforce-Call-Options: client=<name>` so admins can attribute API usage |

## Child clients

`with_auth()` and `tagged()` return cheap child clients. They share the parent's connection
pool, describe cache, observers, usage counters and concurrency limit. Closing a child does
nothing; close the root client.

```python
root = esf.AsyncSalesforce(auth)

# Same login, different tags for logs, usage counters and the budget guard.
backfill = root.tagged(caller="job:backfill", priority="low")

# Another identity (for example a user who logged in through the browser).
user_flow = esf.WebServerFlow(
    domain="https://acme.my.salesforce.com",
    client_id="3MVG9...",
    redirect_uri="https://app.example.com/oauth/callback",
    token_key="user:42",
)
as_user = root.with_auth(user_flow)
print(as_user)

await root.aclose()
```

## Next steps

- Set up the app in Salesforce: [Salesforce setup](authentication/salesforce-setup.md).
- Choose a login flow: [Authentication](authentication/index.md).
- Work with data: [Records](guides/records.md), [Query and search](guides/query.md),
  [Collections](guides/collections.md).
- Handle failures: [Errors](errors.md).
