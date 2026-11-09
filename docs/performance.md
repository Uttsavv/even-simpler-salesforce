# Performance

The defaults aim for few round trips and little work per call. This page lists what the client
does for you and the settings that change it.

| Measure | What happens | Default |
|---|---|---|
| Token reuse | the token stays in memory; refreshes happen only on `401` (or [proactively](authentication/index.md#proactive-refresh-opt-in), if you opt in) | on |
| Connection reuse | one httpx pool per client family (50 connections, 20 kept alive for 30 s); child clients share it | on |
| Response compression | `Accept-Encoding: gzip, deflate` on every request | on |
| Request compression | Collections bodies over 64 KiB are gzip-compressed | on |
| HTTP/2 | `http2=True` with the `[http2]` extra | off |
| Usage tracking | read from response headers; `/limits` only when you ask | on |
| Describe cache | in memory, 1 h TTL, 500 entries (LRU), then revalidated with `If-Modified-Since` (a `304` costs no download) | on |
| Fewer round trips | Collections: 200 records per write, 2,000 ids per read; `page_size` up to 2,000 rows per query page; `count()` | n/a |
| Concurrency | `max_concurrency` limits requests in flight for the client family; Collections run 4 chunks at a time | unlimited / 4 |
| Backoff | exponential with full jitter, capped at 8 s; `Retry-After` is honoured | on |
| JSON | orjson with the `[speed]` extra, otherwise the stdlib; plain `dict`/`list` results | stdlib |

## Share one client

Each root client owns a connection pool, a token, a describe cache and usage counters. Create one
per process (or one per event loop) and pass it around. Don't create one per request. Use
`tagged()` and `with_auth()` children for different tags or identities: they are cheap and share
everything.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth, max_concurrency=10)
reports = sf.tagged(caller="reports")
```

## Bring your own httpx client

To control pools, proxies, certificates or event hooks yourself, inject a client. The library
never closes an injected client, and `timeout=` and `http2=` are ignored because your client's
settings apply:

```python
import httpx

http = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0, connect=5.0),
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
)
custom = esf.AsyncSalesforce(auth, http_client=http)
await custom.query("SELECT Id FROM Lead LIMIT 1")
await custom.aclose()
await http.aclose()
```

Give an `httpx.AsyncClient` to `AsyncSalesforce` and an `httpx.Client` to `Salesforce`.

## JSON codec

With `pip install "even-simpler-salesforce[speed]"`, orjson is used automatically. Both
backends behave the same: NaN and ±Infinity are rejected, `Decimal` is sent as an exact number,
and datetimes become ISO strings. To read numbers as `Decimal` instead of `float`, pass a codec.
The client needs only `dumps(obj) -> bytes` and `loads(bytes)`:

```python
import decimal
import json


class DecimalJson:
    @staticmethod
    def dumps(obj):
        return json.dumps(obj, default=str).encode()

    @staticmethod
    def loads(data):
        return json.loads(data, parse_float=decimal.Decimal)


precise = esf.AsyncSalesforce(auth, json=DecimalJson())
opp = (await precise.query("SELECT Id, Amount FROM Opportunity LIMIT 1")).records[0]
print(type(opp["Amount"]))
await precise.aclose()
```

!!! note
    A custom codec replaces the library's encoder, including its NaN check and its exact
    `Decimal` encoding. The example sends `Decimal` values as strings, which Salesforce accepts
    for number fields. With `model=`, Pydantic `Decimal` fields are usually the simpler choice.

## Fewer calls

- Ask only for the fields you need: `get(id, fields=[...])`, and an explicit `SELECT` list.
- Use `page_size=2000` (the maximum) for large queries, and `query_iter()` instead of
  `query_more()` loops.
- Use Collections for anything with more than a couple of records.
- `count()` costs one call and transfers no rows.
- Leave `describe()` cached. Revalidating a stale entry costs a request but downloads nothing
  when it hasn't changed.

## Benchmarks

`tests/benchmarks/` holds a pytest-benchmark suite. It runs against canned responses through
`httpx.MockTransport` and measures:

- the client's overhead compared with a raw httpx call (sync and async);
- parsing a 2,000-row page;
- `query_iter` over 10 pages;
- chunking 10,000 Collections rows;
- a 100,000-row CSV parse;
- a single-flight refresh with 100 concurrent callers.

Run it with `uvx nox -db uv -s bench`.

```python
await sf.aclose()
```
