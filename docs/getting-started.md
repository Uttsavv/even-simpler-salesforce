# Getting started

even-simpler-salesforce is a typed Salesforce client for Python. It has an async client and a
sync client with the same methods, and it handles login, token refresh and errors for you.

## Install

You need Python 3.10 or newer.

```sh
pip install --pre even-simpler-salesforce
```

`--pre` is needed until 0.1.0 is final, because pip skips release candidates by default.

The import name is `even_simpler_salesforce`. These docs import it as `esf`:

```python
import even_simpler_salesforce as esf

print(esf.__version__)
```

## Make your first call

You need an External Client App in your org. [Salesforce setup](authentication/salesforce-setup.md)
shows how to create one. Then put its credentials in the environment:

```sh
export SF_DOMAIN=https://acme.my.salesforce.com
export SF_CLIENT_ID=3MVG9...
export SF_CLIENT_SECRET=...
```

And make a call:

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

The client sends nothing until the first call. That call gets a token and keeps it in memory.
When the token expires, the client refreshes it and sends the call again.

!!! tip "Top-level `await`"
    Most async examples in these docs use `await` at the top level to stay short, as in
    `python -m asyncio` or a notebook. In a script, wrap them in `async def main()` and call
    `asyncio.run(main())`.

## Async or sync

`AsyncSalesforce` and `Salesforce` have the same methods, arguments and behaviour. Pick the one
that matches your program:

| | `AsyncSalesforce` | `Salesforce` |
|---|---|---|
| Use with | asyncio apps (FastAPI, aiohttp, workers) | scripts, Flask, Django, Celery |
| Context manager | `async with` | `with` |
| Close | `await sf.aclose()` | `sf.close()` |
| `query_iter` | `async for` | `for` |
| Share across | tasks | threads |

Create one client per process and share it. Each client owns a connection pool.

## Records, queries and collections

```python
auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

leads = sf.sobject("Lead")
lead_id = await leads.create({"FirstName": "Ada", "LastName": "Lovelace", "Company": "Engines"})
await leads.update(lead_id, {"Status": "Working"})
lead = await leads.get(lead_id, fields=["Id", "Name", "Status"])

page = await sf.query("SELECT Id, Name FROM Lead WHERE Status = {}", "Working")
print(page.total_size, [r["Name"] for r in page])

result = await sf.collections.update("Lead", [{"Id": lead_id, "Rating": "Hot"}])
result.raise_for_errors()

await sf.aclose()
```

Each of these has its own guide: [Records](guides/records.md),
[Query and search](guides/query.md) and [Collections](guides/collections.md).

## Configure the client

Every option is a keyword argument with a sensible default:

```python
sf = esf.AsyncSalesforce(
    auth,
    api_version="68.0",  # 59.0 or newer
    timeout=esf.Timeouts(connect=5, read=60, write=60, pool=5),
    retry=esf.RetryPolicy(max_attempts=3),
    max_concurrency=20,  # in-flight requests for the whole client family
    client_name="acme-sync",  # Sforce-Call-Options: client=acme-sync
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
| `default_namespace` | `None` | namespace prefix of a managed package, sent as `Sforce-Call-Options: defaultNamespace=<ns>`. Salesforce then leaves the prefix out of field names in responses, so the keys of returned dicts change. See [Default namespace](#default-namespace) |
| `update_mru` | `None` | `False` stops `sf.sobject(...)` calls from adding records to the Recently Viewed list (`Sforce-Mru`); needs `api_version` 60.0 or newer. See [Recently viewed records](guides/records.md#recently-viewed-records) |
| `pubsub_endpoint` | `api.pubsub.salesforce.com:443` | Pub/Sub API host for `sf.pubsub` |

### Default namespace

If your code works with one managed package, set `default_namespace` to the package's
namespace prefix. Then SOQL and record data can name the package's fields without the prefix:
`botId__c` instead of `battle__botId__c`.

```python
packaged = esf.AsyncSalesforce(auth, default_namespace="battle")
await packaged.aclose()
```

Salesforce also leaves the prefix out of the field names it returns, so the dict keys change:
a record has `"botId__c"`, not `"battle__botId__c"`. Code that reads the prefixed keys breaks
when you turn this on. Describe calls ignore the option. The client doesn't send it on SOAP
calls (`sf.leads.convert()`, `sf.leads.merge()`), only `client_name`.

The prefix must have 1 to 15 letters, digits or underscores, start with a letter, and contain
no `__`. Otherwise the client raises `ValueError`. An org without a namespace accepts any value
and ignores it.

## Child clients

Use `tagged()` to label calls in logs and usage counters, and `with_auth()` to act as another
identity. A child shares its parent's connection pool, cache and limits. Close the root
client, not the child.

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

## Optional extras

Install extras for the features that need extra packages:

```sh
pip install --pre "even-simpler-salesforce[jwt,speed]"
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
| `migrate` | libcst | the [`esf migrate`](migrate.md) codemod |
| `all` | every extra above | |

## Project status

even-simpler-salesforce is in beta. Breaking changes can still happen in 0.x minor releases.
Each one is listed in the [changelog](changelog.md) with migration notes, and a deprecated API
keeps working for at least one minor release.

The full test suite runs against a Developer Edition org on API version 68.0, with the web
server flow (PKCE) and refresh-token rotation. Read-only checks also run against a sandbox of an
Enterprise org. Not tested yet: orgs with a namespace, Experience Cloud sites, person accounts,
multi-currency and Shield. If you run it against another setup, please
[share your results](https://github.com/Uttsavv/even-simpler-salesforce/issues).

## Next steps

- [Build a leads API with FastAPI](tutorial.md), step by step.
- [Choose a login flow](authentication/index.md).
- [Work with records](guides/records.md).
- [Handle errors](errors.md).
