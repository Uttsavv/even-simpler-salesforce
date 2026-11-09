# sObject Collections

`sf.collections` creates, updates, upserts, deletes and gets many records per request. Use it
to write more than a few records at a time.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

rows = [{"LastName": f"Imported {i}", "Company": "Acme"} for i in range(450)]
created = await sf.collections.create(rows, sobject="Lead")  # 3 requests: 200 + 200 + 50
print(len(created), len(created.succeeded), len(created.failed))
```

You pass any number of rows. The client splits them into chunks and runs the chunks
concurrently. It returns one result per row, in the order you gave. The client copies your
rows, so it never changes your dicts.

## Create records

Each row needs `attributes.type`, or you pass `sobject=` for all rows. To mix sObject types in
one call, give each row its own `attributes`.

The example at the top of this page creates 450 leads.

## Update records

Each row needs an `Id`. If a row has no `Id`, the client raises `ValueError` before it sends
anything.

```python
ids = [r.id for r in created.succeeded]
await sf.collections.update("Lead", [{"Id": i, "Status": "Working"} for i in ids])
```

## Upsert records

Pass the sObject and the external id field. Each row carries a value for that field.

```python
await sf.collections.upsert(
    "Lead",
    "External_Id__c",
    [{"External_Id__c": "crm-1", "LastName": "Noether", "Company": "Erlangen"}],
)
```

## Delete records

Pass the record ids.

```python
await sf.collections.delete(ids)
```

## Check the results {#results-batchresult-and-recordresult}

If Salesforce rejects some rows, the call still returns normally. Check each `RecordResult` in
the `BatchResult`. It has `ok`, `id` and `errors`. `BatchResult.succeeded` and
`BatchResult.failed` list the results of each kind. `BatchResult.ok` is deprecated: use
`succeeded`.

```python
rows = [
    {"LastName": "Curie", "Company": "Sorbonne"},
    {"LastName": "Meitner"},  # no Company: required field missing
    {"LastName": "Franklin", "Company": "King's College", "Country": "UK"},  # validation rule
]
result = await sf.collections.create(rows, sobject="Lead")
for row, res in zip(rows, result):
    if not res.ok:
        print(row["LastName"], [(e.error_code, e.message) for e in res.errors])
```

To turn any failed row into an exception, call `raise_for_errors()`. It raises
`esf.PartialFailure`:

```python
try:
    result.raise_for_errors()
except esf.PartialFailure as exc:
    print(exc)  # "2 of 3 records failed; first: REQUIRED_FIELD_MISSING: ..."
    print(exc.error_code, len(exc.errors), len(exc.results.failed))
```

Failed rows show up in logs even if you never check. Every Collections `sf.request` event
carries `records_failed`. The [`capture()`](../logging.md#per-request-summary-capture) summary
adds them up in `sf_records_failed`.

## Handle a failed chunk

A whole chunk can fail, for example with a timeout, a 5xx response or an expired session that
the client can't refresh. With `all_or_none=False`, the default, this happens:

1. Every other chunk runs to completion. Nothing keeps running in the background.
2. The call raises `esf.PartialFailure`. Its `.results` is the full `BatchResult`, in input
   order.
3. Rows from chunks that succeeded have their real results.
4. Each row of a failed chunk has `ok=False` and one `ErrorDetail`. Its `extended["outcome"]`
   says what happened.

| `extended["outcome"]` | Meaning |
|---|---|
| `"unknown"` | the request may have reached Salesforce (timeout, reset, 5xx); those rows **may or may not** exist now |
| `"rejected"` | the connection never opened, or Salesforce answered with an error that isn't a 5xx `ServerError` (4xx, budget guard, ...) |

The client doesn't resend writes after a timeout or a 5xx, because the first try may have
committed. So check rows with the outcome `"unknown"` before you send them again:

```python
rows = [{"LastName": f"Batch {i}", "Company": "Acme"} for i in range(600)]
try:
    result = await sf.collections.create(rows, sobject="Lead")
except esf.PartialFailure as exc:
    result = exc.results
    unknown = [
        row
        for row, res in zip(rows, result)
        if not res.ok and res.errors[0].extended.get("outcome") == "unknown"
    ]
    print(f"{len(unknown)} rows may or may not exist; check before re-sending them")
print(len(result.succeeded))
```

`extended["chunk_error"]` holds the name of the exception class. `PartialFailure.__cause__` is
the exception of the first failed chunk.

If every chunk fails, the client can't report a partial result. So it raises the exception of
the first chunk directly, for example `RequestTimeout`. `retrieve` does the same for any failed
chunk.

## Make a write atomic

With `all_or_none=True`, Salesforce rolls back one request if any of its rows fails.

!!! warning "Atomic per chunk, not per call"
    If you pass more than 200 rows, each chunk of 200 is its own transaction. If chunk 3
    fails, chunks 1 and 2 stay committed. For all-or-nothing across more than 200 records, use
    another design: an Apex REST endpoint, the Bulk API with your own compensation, or
    Composite Graph when it ships in 0.3.

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

With `all_or_none=True`, the client runs the chunks one at a time. The first chunk that fails
raises a typed error, such as `RequiredFieldMissing` or `ValidationRuleFailed`. The client
doesn't send later chunks. If you pass more than 200 rows, the client logs a warning on the
`esf` logger.

The exception's `.errors` holds every `ErrorDetail` of the chunk. The primary error is the real
cause. It is not one of the `ALL_OR_NONE_OPERATION_ROLLED_BACK` entries that Salesforce adds
for the other rows.

## Get records by id

`retrieve` gets records by id, in the order you gave. It returns `None` for each id that
Salesforce can't find.

```python
page = await sf.query("SELECT Id FROM Lead LIMIT 5")
wanted = [r["Id"] for r in page] + ["00Q8d0000009999999"]
rows = await sf.collections.retrieve("Lead", wanted, ["Id", "LastName", "Status"])
print(rows[-1])  # None: that id doesn't exist
```

`retrieve` sends up to 2,000 ids per request. It uses a POST, so long lists of ids don't hit
URL limits. It is a read, so the client retries it after timeouts and 5xx responses.

## Set the concurrency

By default, at most 4 chunks are in flight at once. Pass `concurrency=` to change this for one
call. To change it for the client, set `sf.collections.concurrency = 8`.

```python
result = await sf.collections.update(
    "Lead", [{"Id": r.id, "Rating": "Warm"} for r in created.succeeded], concurrency=2
)
print(len(result))
await sf.aclose()
```

The client-wide `max_concurrency=` limit applies on top of this. The sync client runs chunks on
a thread pool of the same size. `capture()` and `call_context()` still apply inside it.

## Methods

| Method | REST call | Chunk size | Returns |
|---|---|---|---|
| `create(rows, *, sobject=None, all_or_none=False, concurrency=None)` | `POST composite/sobjects` | 200 | `BatchResult[RecordResult]` |
| `update(sobject, rows, *, all_or_none=False, concurrency=None)` | `PATCH composite/sobjects` | 200 | `BatchResult[RecordResult]` |
| `upsert(sobject, field, rows, *, all_or_none=False, concurrency=None)` | `PATCH composite/sobjects/{o}/{field}` | 200 | `BatchResult[RecordResult]` |
| `delete(ids, *, all_or_none=False, concurrency=None)` | `DELETE composite/sobjects?ids=` | 200 | `BatchResult[RecordResult]` |
| `retrieve(sobject, ids, fields, *, concurrency=None)` | `POST composite/sobjects/{o}` | 2,000 | `list[dict \| None]` |

## Results

`BatchResult` is a sequence with one `RecordResult` per input row, in input order.

| `RecordResult` field | Meaning |
|---|---|
| `ok` | the row succeeded |
| `id` | record id, if Salesforce returned one |
| `created` | upsert only: `True` for an insert, `False` for an update |
| `errors` | tuple of `esf.ErrorDetail(error_code, message, fields, extended)` |

| `BatchResult` member | Meaning |
|---|---|
| `.succeeded` | list of the results that succeeded (`.ok` is a deprecated alias) |
| `.failed` | list of the results that failed |
| `.results` | every result, in input order |
| `.operation`, `.sobject` | what the call did, and on which sObject |
| `raise_for_errors()` | raises `esf.PartialFailure` if any row failed |
| `len()`, indexing, iteration | as for a sequence |

## How it works

The client sends rows to the
[sObject Collections](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobjects_collections.htm)
resource in chunks of 200, or 2,000 for `retrieve`. If a request body is larger than 64 KiB,
the client compresses it with gzip.

The client doesn't resend writes after a timeout or a 5xx, because the first try may have
committed. [ADR 0001](../adr/0001-retry-safety.md) explains why. The client still retries row
locks (`UNABLE_TO_LOCK_ROW`) and rate limits with `Retry-After`. Salesforce rejected those
requests without running them.
