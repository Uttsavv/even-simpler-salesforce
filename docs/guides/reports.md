# Reports and dashboards

`sf.reports` runs the reports your admins built in Salesforce and reads dashboards. Use it
when the numbers you need already live in a report, with its filters and groupings.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

result = await sf.reports.run("00O5g00000ABCDeEAK")
for row in result.rows():
    print(row["STAGE_NAME"], row["OPPORTUNITY_NAME"])  # Prospecting Acme - 200 units
```

`run()` returns an `esf.ReportResult`. `rows()` gives one dict per record. The keys are the
column API names of the report, such as `OPPORTUNITY_NAME`.

## Read the totals

`summary_rows()` gives the aggregates of every grouping, every subtotal and the grand total.
Each dict holds the grouping values and one key per aggregate, such as `s!AMOUNT` for "Sum of
Amount" and `RowCount` for the record count.

```python
for row in result.summary_rows():
    print(row.get("STAGE_NAME", "Total"), row["RowCount"])
# Prospecting 2
# Closed Won 1
# Total 3
```

If you need only the totals, pass `include_details=False`. Salesforce then sends no detail
rows, so the response is smaller.

```python
totals = await sf.reports.run("00O5g00000ABCDeEAK", include_details=False)
print(totals.rows())  # []
print(totals.summary_rows()[-1]["s!AMOUNT"])  # {'amount': 4000.0, 'currency': None}
```

## Filter a run

Pass `filters` to replace the saved filters of the report for this run only. Each filter is a
dict with `column`, `operator` and `value`, as Salesforce defines them.

```python
result = await sf.reports.run(
    "00O5g00000ABCDeEAK",
    [{"column": "STAGE_NAME", "operator": "equals", "value": "Prospecting"}],
)
```

To find the column names and the saved filters, call `describe()`. It returns the report
metadata as Salesforce sends it.

```python
meta = await sf.reports.describe("00O5g00000ABCDeEAK")
print(meta["reportMetadata"]["reportFormat"])  # SUMMARY
print(meta["reportMetadata"]["detailColumns"])  # ['ACCOUNT_NAME', 'OPPORTUNITY_NAME', 'AMOUNT']
```

## Run a slow report

A synchronous run must finish within the API timeout of Salesforce. For a slow report, start
an asynchronous run with `run_async()`. Then wait for the results with `instance()`.

```python
run = await sf.reports.run_async("00O5g00000ABCDeEAK")
print(run.status)  # New
result = await sf.reports.instance(run.report_id, run.id, timeout=300)
print(len(result.rows()))  # 3
```

`instance()` asks Salesforce for the status every `poll_interval` seconds, 2 by default. It
returns when the status is `Success`.

| Outcome | What `instance()` does |
|---|---|
| Status `Success` | returns the `ReportResult` |
| Status `Error` | raises `esf.ReportRunFailed` |
| Still `New` or `Running` after `timeout` seconds | raises `esf.ReportTimeout`; the run keeps going |
| Any other status | raises `esf.UnexpectedResponse` |

If you get `ReportTimeout`, call `instance()` again with the same ids to keep waiting. Don't
start a new run.

## Read a dashboard

`sf.reports.dashboards.get()` returns the dashboard metadata and the data of each component.
Each component has a `reportResult` in the same format as a report run. Wrap it in
`esf.ReportResult` to read its rows.

```python
dashboard = await sf.reports.dashboards.get("01Z5g00000ABCDeEAK")
for component in dashboard["componentData"]:
    print(component["status"]["refreshStatus"])  # IDLE
    print(esf.ReportResult(component["reportResult"]).summary_rows()[0]["RowCount"])  # 2
```

## Refresh a dashboard

`refresh()` asks Salesforce to refresh every component. It returns at once. The refresh runs in
Salesforce.

```python
started = await sf.reports.dashboards.refresh("01Z5g00000ABCDeEAK")
print(started["statusUrl"])  # /services/data/v68.0/analytics/dashboards/01Z5g00000ABCDeEAK/status
```

Call `get()` later to read the new data. A component is done when its `refreshStatus` is
`IDLE`.

## Test code that uses reports

`FakeSalesforce` answers reports and dashboards that you script with `fake.on_report()` and
`fake.on_dashboard()`. An asynchronous run finishes at once.

```python
from even_simpler_salesforce.testing import FakeSalesforce

def test_pipeline_total() -> None:
    fake = FakeSalesforce()
    fake.on_report("00O5g00000ABCDeEAK", {
        "reportMetadata": {"reportFormat": "TABULAR", "detailColumns": ["NAME"], "aggregates": ["RowCount"]},
        "factMap": {"T!T": {"aggregates": [{"label": "1", "value": 1}],
                            "rows": [{"dataCells": [{"label": "Acme", "value": "Acme"}]}]}},
    })
    result = fake.reports.run("00O5g00000ABCDeEAK")
    assert result.rows() == [{"NAME": "Acme"}]
    assert result.summary_rows() == [{"RowCount": 1}]
```

`result` can also be a callable `(filters, include_details) -> body`, or an exception to
raise, such as `esf.Forbidden("no access")`.

## How rows map to the fact map

Salesforce returns a report run as a fact map. Each key is `<row key>!<column key>`. A key is
the key of a grouping, such as `0` or `0_1`, or `T` for a total. The helpers read it as
follows.

| Report format | `rows()` reads | `summary_rows()` reads |
|---|---|---|
| Tabular | the rows of `T!T` | `T!T` |
| Summary | the rows of each deepest row grouping, such as `0_0!T` | every row grouping, then `T!T` |
| Matrix | the rows of each deepest cell, such as `0_0!0_0` | every pair of row and column groupings, with the totals |

- A row from `rows()` starts with the grouping values of its cell, row groupings first. Then
  come the detail columns, in report order.
- Each grouping value is the `value` of the grouping, for example `"2026-10-01"` for a date
  grouping by month. A detail value is the `value` of the cell. A currency cell has the value
  `{"amount": ..., "currency": ...}`. For the display text, read `result.raw`.
- In `summary_rows()`, a subtotal has no keys for the deeper groupings. The grand total has
  only aggregates.
- Rows are in the order of the groupings. Within a grouping, they keep the report order.
- If Salesforce leaves out an empty matrix cell, the helpers skip it.

## How it works

- `run()` sends `GET analytics/reports/{id}?includeDetails=true`. With `filters`, it sends a
  `POST` with the body `{"reportMetadata": {"reportFilters": [...]}}`.
- A run reads data. The client resends it after a timeout or a `5xx`, even as a `POST`.
- `run_async()` sends `POST analytics/reports/{id}/instances`. Each call creates a report
  instance, so the client never resends it after it may have reached Salesforce.
- `instance()` sends `GET analytics/reports/{id}/instances/{instance}`. Each poll is one API
  call.
- `dashboards.get()` sends `GET analytics/dashboards/{id}`. `dashboards.refresh()` sends
  `PUT` to the same path and is never resent.

## Limitations

- A synchronous run returns at most 2,000 detail rows. If `result.all_data` is `False`, the
  rows are cut short. Add filters, or use `sf.query_iter()` for the records.
- `rows()` and `summary_rows()` don't read joined reports (`MULTI_BLOCK`). They raise
  `ValueError`; read `result.raw` instead.
- To change the groupings or the aggregates of a run, send your own `reportMetadata` with
  [`sf.request()`](requests.md).
