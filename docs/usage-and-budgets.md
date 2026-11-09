# Usage and budgets

Salesforce limits the API calls an org can make in a rolling 24 hours. Every integration in the
org shares that limit. The client tracks your usage at no extra cost, and a budget can stop
low-priority calls before the org runs out.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)
await sf.query("SELECT Id FROM Lead LIMIT 1")
await sf.sobject("Account").describe()

usage = sf.usage.snapshot()
print(f"{usage.api_used}/{usage.api_max} ({usage.api_fraction:.1%}) as of {usage.as_of}")
print(usage.calls_by_operation)  # {'query': 1, 'sobject.describe': 1}
```

`api_used` and `api_max` count the calls of the whole org, from all integrations. The
`calls_by_*` counters count only the calls of this client family in this process. Until the
first response arrives, the usage values are `None`. The [`UsageSnapshot`](#usagesnapshot)
table lists every field.

## Caller tags and priority

Tag your calls to see where your usage goes. The budget guard also uses the priority to tell
important calls from calls that can wait.

```python
backfill = sf.tagged(caller="job:backfill", priority="low")
await backfill.query("SELECT Id FROM Lead LIMIT 10")

with esf.call_context(caller="route:/leads", priority="high"):
    await sf.query("SELECT Id FROM Lead LIMIT 10")  # tagged too, across awaits

print(sf.usage.snapshot().calls_by_caller)  # {'-': 2, 'job:backfill': 1, 'route:/leads': 1}
```

- `sf.tagged(caller=..., priority=...)` returns a child client. It shares everything with `sf`
  and adds the tags to each call.
- `esf.call_context(caller=..., priority=...)` sets the tags for every call inside the block,
  in any client. It uses a context variable, so it works across `await`. The Collections
  threads of the sync client inherit it.
- The priority is `"low"`, `"normal"` or `"high"`. The default is `"normal"`.
- The tags of a `tagged()` child win over `call_context()`, and `call_context()` wins over the
  default.
- The tags appear in `sf.request` events as `caller` and `priority`.

In `calls_by_caller`, `"-"` counts the calls without a caller tag.

## Name your integration

To show your integration's name to Salesforce admins, pass `client_name` to the client:

```python
named = esf.AsyncSalesforce(auth, client_name="acme-sync")
await named.aclose()
```

The client sends it as `Sforce-Call-Options: client=acme-sync` on every API call. The name can
have up to 64 characters: letters, digits and `_ . : -`. If you set your own
`Sforce-Call-Options` header on a call, the client merges the two.

We have not yet checked on a live org where admins see the `client` value. This is
verification item V20 in the spec.

## Budgets

To get threshold events and a guard, pass `budget=esf.Budget(...)`. Both use the latest usage
header, so neither makes extra calls.

```python
budget = esf.Budget(
    warn_at=(0.5, 0.8, 0.95),  # emit sf.usage.threshold once per threshold per UTC day
    pause_low_priority_above=0.8,  # BudgetExceeded for priority="low" calls above 80 %
    block_all_above=0.98,  # BudgetExceeded for everything except priority="high"
)
guarded = esf.AsyncSalesforce(auth, budget=budget)
await guarded.aclose()
```

| Field | Default | Description |
|---|---|---|
| `warn_at` | `(0.5, 0.8, 0.95)` | if the fraction reaches a threshold, emit `sf.usage.threshold` with `threshold`, `fraction`, `used` and `max`; at most once per threshold per UTC day |
| `pause_low_priority_above` | `0.8` | above this fraction, `priority="low"` calls raise `esf.BudgetExceeded` before the client sends them |
| `block_all_above` | `None` | above this fraction, every call except `priority="high"` raises `BudgetExceeded` |

Without `budget=`, you get no threshold events and no guard. The client still tracks usage.

### Pause a job when the budget runs low

`BudgetExceeded` is a `LimitError` with `error_code="BUDGET_EXCEEDED"`. Its `.usage` holds the
snapshot, so a job can sleep and try again later:

```python
from even_simpler_salesforce.testing import FakeSalesforce

fake = FakeSalesforce(budget=esf.Budget())  # the in-memory fake applies budgets too
fake.set_api_usage(85_000, 100_000)  # pretend the header said 85 %

job = fake.tagged(caller="job:nightly", priority="low")
try:
    job.query("SELECT Id FROM Lead")
except esf.BudgetExceeded as exc:
    print(f"paused at {exc.usage.api_fraction:.0%}")  # paused at 85%
```

## Get every org limit

`sf.limits()` gets every org limit, such as API calls, storage, Bulk and e-mail. It costs one
API call, and the client never calls it for you.

```python
limits = await sf.limits()
daily = limits["DailyApiRequests"]
print(daily.max, daily.remaining, daily.used)
print(sorted(limits)[:3])
```

It returns `dict[str, esf.Limit]`. A `Limit` has `max`, `remaining`, `used` and `details`.
`details` holds any other keys in the entry, such as the breakdown per app. `limits()` also
updates the usage tracker from `DailyApiRequests`.

### Poll the limits

To keep the numbers fresh while the process is idle, start the poller:

```python
sf.start_limits_poller(interval=900)
await sf.aclose()  # stops the poller
```

- The interval must be at least 300 seconds. Each poll costs one API call.
- Each client family has one poller. It stops when you close the root client.
- With `AsyncSalesforce`, call it from inside a running event loop.
- With `Salesforce`, the poller runs in a daemon thread.

## Sources

| Source | Gives | Cost |
|---|---|---|
| `Sforce-Limit-Info` header on every response | daily API calls used and max for the whole org, and `api-bursts` if present | free |
| library counters | calls by operation, sObject and caller; errors by code; retries; refreshes | free |
| `sf.limits()` | every org limit (API, storage, Bulk, e-mail, ...) | 1 API call |
| `sf.start_limits_poller()` | `sf.limits()` on a timer, at least 5 minutes apart | 1 API call per poll |

## `UsageSnapshot`

| Field | Description |
|---|---|
| `api_used`, `api_max`, `api_fraction` | from the latest `Sforce-Limit-Info` header; org-wide, all integrations |
| `api_bursts_used`, `api_bursts_max` | `api-bursts` from the same header, when Salesforce sends it |
| `as_of` | when the client saw that header (UTC) |
| `calls_by_operation`, `calls_by_sobject`, `calls_by_caller` | calls made by this client family in this process (`"-"` = no caller tag) |
| `errors_by_code` | failed calls by `error_code`, or by exception class name if there is no code |
| `retries`, `refreshes` | retries made, and token refreshes after a rejected or expired token |
| `projected_exhaustion` | when the org reaches `api_max` at the current rate, or `None` |

## How it works

Each REST response carries a `Sforce-Limit-Info: api-usage=used/max` header. The client reads
it, so tracking costs no extra calls.

The projection in `projected_exhaustion` is linear. It uses the header values from the last
hour. It stays `None` until at least two samples, one minute apart, show growing usage.

Some calls send nothing, so they don't change the header values:

- describe calls that the cache answers;
- calls that the budget guard blocks.
