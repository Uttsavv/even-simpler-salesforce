# Bulk API 2.0

`sf.bulk` loads and reads large numbers of records with Bulk API 2.0 jobs. Use it for
thousands to millions of records, where sObject Collections would need too many calls.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

rows = [{"LastName": f"Lead {n}", "Company": "Acme"} for n in range(1000)]
job = await sf.bulk.ingest("Lead", "insert", rows=rows)
info = await job.wait()
print(info.state, info.records_processed, info.records_failed)  # JobComplete 1000 0
```

A job runs in Salesforce, not in your process. `ingest()` creates the job, uploads the data and
closes the upload. `wait()` then polls until the job is finished.

## Load records

Pass the rows as dicts, as the path of a CSV file, or as CSV `bytes`. The operation is
`"insert"`, `"update"`, `"upsert"`, `"delete"` or `"hardDelete"`.

```python
with open("leads.csv", "w", encoding="utf-8") as f:
    f.write("External_Id__c,LastName,Company\next-1,Doe,Acme\next-2,Roe,Globex\n")

job = await sf.bulk.ingest("Lead", "upsert", external_id_field="External_Id__c", rows="leads.csv")
await job.wait()

await sf.bulk.ingest("Lead", "update", rows=b"Id,Status\n00Q8d0000000009AAA,Working\n")
```

The client streams the upload. It never reads a whole file into memory. Rows from an iterable go
to a temporary file first, so a generator of a million rows is fine.

A CSV file must be UTF-8. If your file uses `CRLF` line endings or another delimiter, say so with
`line_ending="CRLF"` or `column_delimiter="TAB"`. The client checks the header row before it
sends anything.

## Empty values and null

In Bulk CSV, an empty cell leaves the field unchanged and `#N/A` sets it to null. For dict rows
the client writes the cells for you.

| In the dict | In the CSV | Effect |
|---|---|---|
| `"Description": None` or `""` | `#N/A` | sets the field to null |
| key left out | empty cell | leaves the field unchanged |
| `"Description": "#N/A"` | | raises `ValueError`, because the meaning is unclear |

The keys of the first row are the columns. A later row may leave a key out, but it can't add one.

```python
await sf.bulk.ingest(
    "Lead",
    "update",
    rows=[
        {"Id": "00Q8d0000000009AAA", "Phone": None},  # clears Phone
        {"Id": "00Q8d0000000010AAA"},  # changes nothing
    ],
)
```

## Wait for a job

`wait()` polls the job status. By default it waits 2 seconds, then doubles the wait up to 30
seconds. It gives up after `timeout` seconds, 3600 by default.

```python
job = await sf.bulk.ingest("Lead", "insert", rows=rows)
info = await job.wait(poll=esf.Backoff(start=2, max=30), timeout=3600)
print(info.total_processing_ms)
```

If the job ends `Failed` or `Aborted`, `wait()` raises `esf.BulkJobFailed`. If the time runs out,
it raises `esf.BulkJobTimeout`. The job keeps running in Salesforce, so you can call `wait()`
again.

`job.info` holds the status from the latest call. Call `refresh()` to get it again without
waiting.

To show progress, add an [observer](../logging.md#write-your-own-observer) for the `sf.bulk.job` event. The
client emits it when the job state changes, and on each poll where `records_processed` or
`records_failed` changed.

## Read the results

A job that ends `JobComplete` can still have rows that failed. Read them after `wait()`.

```python
async for row in job.successful_results():
    print(row["sf__Id"], row["sf__Created"])

async for failure in job.failed_results():
    print(failure.error.error_code, failure.error.message, failure.fields)
```

Each failed row is an `esf.BulkFailure`. Its `error` is an `esf.ErrorDetail` with the status code,
the message and the fields that Salesforce reported. `fields` holds the columns you uploaded.

If a job failed or was aborted, `unprocessed_records()` returns the rows that Salesforce didn't
process. Call `abort()` to stop a job that is still running.

## Query records

`sf.bulk.query()` runs a query job, waits for it and yields every row. Rows are dicts of
strings. An empty value is `None`, and a parent field keeps its dotted name.

```python
async for row in sf.bulk.query("SELECT Id, LastName, Status FROM Lead WHERE Status = {}", "Open"):
    print(row["Id"], row["LastName"])
```

`{}` placeholders work as in `sf.query()`. Pass `include_deleted=True` to include deleted and
archived records, like `queryAll`.

The client reads each result page as it arrives, so memory stays flat for any result size. With
`parallel_downloads=4` it downloads up to four pages at the same time. The rows still come in
order.

```python
count = 0
async for row in sf.bulk.query("SELECT Id FROM Lead", parallel_downloads=4):
    count += 1
```

## Compound fields

Bulk queries can't read compound address and geolocation fields, such as `BillingAddress`.
`expand_compound_fields()` replaces them with their component fields.

```python
fields = await sf.bulk.expand_compound_fields("Account", ["Id", "Name", "BillingAddress"])
print(fields)  # ['Id', 'Name', 'BillingStreet', 'BillingCity', ...]
```

It reads the describe of the object, from the describe cache when it is fresh. If a query job
fails because of a compound field, `BulkJobFailed.hint` names the component fields.

## All fields

Query jobs don't accept `FIELDS(ALL)` or `FIELDS(CUSTOM)`. If the query uses one of them,
`sf.bulk.query()` raises `ValueError` before it creates a job. Build the field list from the
describe instead:

```python
describe = await sf.sobject("Account").describe()
names = await sf.bulk.expand_compound_fields("Account", [f["name"] for f in describe["fields"]])
async for row in sf.bulk.query(f"SELECT {', '.join(names)} FROM Account"):
    pass  # every field the user can read, compound fields as components
```

See [Create a Query Job](https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/query_create_job.htm).

## Methods

| Method | Returns | Description |
|---|---|---|
| `sf.bulk.ingest(sobject, operation, rows=...)` | ingest job | create a job, upload the data, close the upload |
| `job.wait(poll=, timeout=)` | `BulkJobInfo` | poll until the job is finished |
| `job.refresh()` | `BulkJobInfo` | get the status once |
| `job.abort()` | `BulkJobInfo` | abort the job |
| `job.successful_results()` | rows | saved rows with `sf__Id` and `sf__Created` |
| `job.failed_results()` | `BulkFailure` items | rejected rows with the parsed error |
| `job.unprocessed_records()` | rows | rows that Salesforce didn't process |
| `sf.bulk.query(soql, include_deleted=, parallel_downloads=)` | rows | run a query job and stream the rows |
| `sf.bulk.expand_compound_fields(sobject, fields)` | `list[str]` | swap compound fields for their components |

## Errors

| Exception | When |
|---|---|
| `esf.BulkJobFailed` | the job ended `Failed` or `Aborted`; `.job_id`, `.state`, `.error_message` |
| `esf.BulkJobTimeout` | `wait()` ran out of time; the job keeps running |
| `esf.UnexpectedResponse` | Salesforce reported a job state that the client doesn't know |
| `ValueError` | invalid arguments or data, raised before anything is sent |

## Testing

`FakeSalesforce` supports `bulk`. An ingest job applies its rows to the fake's records at once and
is `JobComplete`. A query returns the rows you scripted with `on_query()`, as strings.

```python
from even_simpler_salesforce.testing import FakeSalesforce


def test_import(esf_fake: FakeSalesforce) -> None:
    job = esf_fake.bulk.ingest("Lead", "insert", rows=[{"LastName": "Doe", "Company": "Acme"}])
    assert job.wait().records_processed == 1
    assert esf_fake.records("Lead")[0]["LastName"] == "Doe"
```

## How it works

- Salesforce accepts at most 150 MB of base64 data per upload, which is about 100 MB of CSV. If
  your data is larger, the client splits it over several jobs. Each job gets the header row and
  only whole records. The job object stands for all of them: `job.ids` lists them, and the
  results come from each job in turn.
- If an upload fails, the client aborts that job and raises the error. Jobs it uploaded before
  keep running, and the error message names them.
- Results must be read with the API version that created the job, or Salesforce returns `409`.
  The job keeps that version for every later call.
- The client sends uploads as `text/csv; charset=UTF-8`. It reads result files as UTF-8,
  whatever charset the response names.
- Parallel downloads use the `resultPages` resource, which gives up to five page URLs at a time.
  Values of `parallel_downloads` above 5 don't download more.
- Every change of job state or progress emits an `sf.bulk.job` [event](../logging.md#events).
