# Performance

The client's defaults keep round trips few and the work per call small. This page shows how to
get the most from them, and which settings change them.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth, max_concurrency=10)
reports = sf.tagged(caller="reports")
```

## Share one client

Create one client per process, or one per event loop, and pass it around. Don't create one per
request. Each root client owns a connection pool, a token, a describe cache and usage counters.

For other tags or identities, use `tagged()` and `with_auth()` children. They are cheap and
share everything with the root client.

`max_concurrency` limits the requests in flight for the whole client family. By default there
is no limit.

## Make fewer calls

- Ask only for the fields you need: `get(id, fields=[...])`, and an explicit `SELECT` list.
- For large queries, use `page_size=2000`, the maximum.
- Use `query_iter()` instead of `query_more()` loops.
- Use Collections for anything with more than a couple of records.
- To count rows, use `count()`. It costs one call and transfers no rows.
- Leave `describe()` cached. If a cached entry is stale, the client checks it again with one
  request. If the entry has not changed, that request downloads nothing.

## Bring your own httpx client

To control pools, proxies, certificates or event hooks, pass your own httpx client:

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

- Give an `httpx.AsyncClient` to `AsyncSalesforce` and an `httpx.Client` to `Salesforce`.
- The library never closes a client you pass in. Close it yourself.
- The client ignores `timeout=` and `http2=`, because the settings of your httpx client apply.

## Use a faster JSON codec

Install the `[speed]` extra, and the client uses orjson:

```sh
pip install "even-simpler-salesforce[speed]"
```

orjson and the standard library behave the same:

- they reject NaN and ±Infinity;
- they send `Decimal` as an exact number;
- they turn datetimes into ISO strings.

### Read numbers as `Decimal`

To read numbers as `Decimal` instead of `float`, pass your own codec. It needs only
`dumps(obj) -> bytes` and `loads(bytes)`.

A custom codec replaces the library's encoder, including its NaN check and its exact `Decimal`
encoding. The example below sends `Decimal` values as strings, which Salesforce accepts for
number fields. With `model=`, Pydantic `Decimal` fields are often simpler.

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

## Run the benchmarks

`tests/benchmarks/` holds a pytest-benchmark suite. Run it with this command:

```sh
uvx nox -db uv -s bench
```

The suite runs against canned responses through `httpx.MockTransport`. It measures these:

- the client's overhead compared with a raw httpx call, for async and sync;
- parsing a page of 2,000 rows;
- `query_iter` over 10 pages;
- chunking 10,000 Collections rows;
- parsing a CSV of 100,000 rows;
- a single-flight refresh with 100 concurrent callers.

## Defaults

| Measure | Default | Description |
|---|---|---|
| Token reuse | on | the token stays in memory; the client refreshes it only on `401`, or [proactively](authentication/index.md#proactive-refresh-opt-in) if you opt in |
| Connection reuse | on | one httpx pool per client family (50 connections, 20 kept alive for 30 s); child clients share it |
| Response compression | on | `Accept-Encoding: gzip, deflate` on every request |
| Request compression | on | the client compresses Collections bodies over 64 KiB with gzip |
| HTTP/2 | off | `http2=True` with the `[http2]` extra |
| Usage tracking | on | from response headers; `/limits` only when you ask |
| Describe cache | on | in memory, 1 h TTL, 500 entries (LRU), then checked again with `If-Modified-Since`; a `304` costs no download |
| Fewer round trips | n/a | Collections: 200 records per write, 2,000 ids per read; `page_size` up to 2,000 rows per query page; `count()` |
| Concurrency | unlimited / 4 | `max_concurrency` limits requests in flight for the client family; Collections run 4 chunks at a time |
| Backoff | on | exponential with full jitter, capped at 8 s; the client honours `Retry-After` |
| JSON | stdlib | orjson with the `[speed]` extra, otherwise the stdlib; results are plain `dict` and `list` |

```python
await sf.aclose()
```
