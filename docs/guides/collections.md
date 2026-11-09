# sObject Collections

`sf.collections` writes or reads many records per request with the
[sObject Collections](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections.htm)
resource. You pass any number of rows. The client splits them into chunks, runs the chunks
concurrently and returns one result per row **in the order you gave**.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)
```

| Method | REST call | Chunk size | Returns |
|---|---|---|---|
| `create(rows, *, sobject=None, all_or_none=False, concurrency=None)` | `POST composite/sobjects` | 200 | `BatchResult[RecordResult]` |
| `update(sobject, rows, *, all_or_none=False, concurrency=None)` | `PATCH composite/sobjects` | 200 | `BatchResult[RecordResult]` |
| `upsert(sobject, field, rows, *, all_or_none=False, concurrency=None)` | `PATCH composite/sobjects/{o}/{field}` | 200 | `BatchResult[RecordResult]` |
| `delete(ids, *, all_or_none=False, concurrency=None)` | `DELETE composite/sobjects?ids=` | 200 | `BatchResult[RecordResult]` |
| `retrieve(sobject, ids, fields, *, concurrency=None)` | `POST composite/sobjects/{o}` | 2,000 | `list[dict \| None]` |

## Writing

```python
rows = [{"LastName": f"Imported {i}", "Company": "Acme"} for i in range(450)]
created = await sf.collections.create(rows, sobject="Lead")      # 3 requests: 200 + 200 + 50
print(len(created), len(created.ok), len(created.failed))

ids = [r.id for r in created.ok]
await sf.collections.update("Lead", [{"Id": i, "Status": "Working"} for i in ids])
await sf.collections.upsert(
    "Lead", "External_Id__c",
    [{"External_Id__c": "crm-1", "LastName": "Noether", "Company": "Erlangen"}],
)
await sf.collections.delete(ids)
```

- `create`: each row needs `attributes.type`, or pass `sobject=` for all rows. Rows of
  different types may be mixed when each row has its own `attributes`.
- `update`: each row needs an `Id`, otherwise `ValueError` is raised before anything is sent.
- `upsert`: each row carries the external id field.
- Rows are copied. Your dicts are never modified.
- Request bodies larger than 64 KiB are gzip-compressed.

## Results: `BatchResult` and `RecordResult`

`BatchResult` is a sequence with one `RecordResult` per input row, in input order:

| `RecordResult` field | Meaning |
|---|---|
| `ok` | the row succeeded |
| `id` | record id (when Salesforce returned one) |
| `created` | upsert only: `True` for an insert, `False` for an update |
| `errors` | tuple of `esf.ErrorDetail(error_code, message, fields, extended)` |

`BatchResult` also has `.ok` and `.failed` (lists of results), `.results`, indexing, `len()`,
iteration, `.operation` and `.sobject`.

```python
rows = [
    {"LastName": "Curie", "Company": "Sorbonne"},
    {"LastName": "Meitner"},                           # no Company: required field missing
    {"LastName": "Franklin", "Company": "King's College", "Country": "UK"},  # validation rule
]
result = await sf.collections.create(rows, sobject="Lead")
for row, res in zip(rows, result):
    if not res.ok:
        print(row["LastName"], [(e.error_code, e.message) for e in res.errors])
```

**Row-level failures don't raise.** A request that succeeded with some rows rejected returns
normally. Check `.failed`, or call `raise_for_errors()` to turn any failure into
`esf.PartialFailure`:

```python
try:
    result.raise_for_errors()
except esf.PartialFailure as exc:
    print(exc)                 # "2 of 3 records failed; first: REQUIRED_FIELD_MISSING: ..."
    print(exc.error_code, len(exc.errors), len(exc.results.failed))
```

Failures are visible in logs even if you never check: every Collections `sf.request` event
carries `records_failed`, and the [`capture()`](../logging.md#per-request-summary-capture)
summary adds them up in `sf_records_failed`.

## When a whole chunk fails

A chunk's request can fail as a whole, for example with a timeout, a 5xx response or an
expired session that couldn't be refreshed. With `all_or_none=False` (the default):

1. every other chunk still runs to completion. Nothing is left running in the background;
2. then the call raises **`esf.PartialFailure`**. Its `.results` is the full ordered
   `BatchResult`. Rows from successful chunks have their real results. Each row of a failed
   chunk has `ok=False` and one `ErrorDetail` whose `extended` says what happened:

    | `extended["outcome"]` | Meaning |
    |---|---|
    | `"unknown"` | the request may have reached Salesforce (timeout, reset, 5xx). Those rows **may or may not** have been written |
    | `"rejected"` | anything else: the connection was never made, or Salesforce answered with an error that isn't a 5xx `ServerError` (4xx, budget guard, ...) |

    `extended["chunk_error"]` holds the exception class name. `PartialFailure.__cause__` is the
    first chunk's exception.

If **every** chunk fails, or the call is `retrieve`, nothing partial can be reported, so the
first chunk's exception is raised directly (e.g. `RequestTimeout`).

```python
rows = [{"LastName": f"Batch {i}", "Company": "Acme"} for i in range(600)]
try:
    result = await sf.collections.create(rows, sobject="Lead")
except esf.PartialFailure as exc:
    result = exc.results
    unknown = [
        row for row, res in zip(rows, result)
        if not res.ok and res.errors[0].extended.get("outcome") == "unknown"
    ]
    print(f"{len(unknown)} rows may or may not exist; check before re-sending them")
print(len(result.ok))
```

Writes are **not** resent automatically after a timeout or 5xx, because the first attempt may
have committed (see [ADR 0001](../adr/0001-retry-safety.md)). Row locks (`UNABLE_TO_LOCK_ROW`)
and rate limits with `Retry-After` are still retried, because Salesforce rejected those requests
without running them.

## `all_or_none=True`

Salesforce rolls back **one request** when any of its rows fails. The library sends at most 200
rows per request, so:

!!! danger "Atomic per chunk, not per call"
    With more than 200 rows, each chunk of 200 is its own transaction. When chunk 3 fails,
    **chunks 1 and 2 are already committed**. The library logs a warning on the `esf` logger
    whenever `all_or_none=True` is used with more than 200 rows. If you need all-or-nothing
    across more than 200 records, use a different design (an Apex REST endpoint, Bulk API with
    your own compensation, or Composite Graph when it ships in 0.3).

With `all_or_none=True`, chunks run **one at a time** and the first failing chunk raises a
typed error, such as `RequiredFieldMissing` or `ValidationRuleFailed`. Later chunks are not
sent. The exception's `.errors` holds every `ErrorDetail` of that chunk. The primary error is
the real cause, not Salesforce's `ALL_OR_NONE_OPERATION_ROLLED_BACK` entries for the innocent
rows.

```python
try:
    await sf.collections.create(
        [{"LastName": "Ride", "Company": "NASA"}, {"LastName": "Jemison"}],
        sobject="Lead",
        all_or_none=True,
    )
except esf.RequiredFieldMissing as exc:
    print(exc.fields, [e.error_code for e in exc.errors])
```

## Reading: `retrieve`

```python
page = await sf.query("SELECT Id FROM Lead LIMIT 5")
wanted = [r["Id"] for r in page] + ["00Q8d0000009999999"]
rows = await sf.collections.retrieve("Lead", wanted, ["Id", "LastName", "Status"])
print(rows[-1])   # None: that id doesn't exist
```

`retrieve` sends up to 2,000 ids per request (a POST, so long id lists don't hit URL limits),
keeps the input order and returns `None` for ids that weren't found. It is a read, so it is
retried after timeouts and 5xx responses.

## Concurrency

Chunks run with at most `concurrency` requests in flight (default **4**; set it per call, or for
the client with `sf.collections.concurrency = 8`). The client-wide `max_concurrency=` limit
applies on top of this. The sync client runs chunks on a thread pool of the same size, and
`capture()` and `call_context()` still apply inside it.

```python
result = await sf.collections.update(
    "Lead", [{"Id": r.id, "Rating": "Warm"} for r in created.ok], concurrency=2
)
print(len(result))
await sf.aclose()
```
