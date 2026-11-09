# Usage and budgets

Salesforce limits how many API calls an org can make in a rolling 24 hours, and every
integration in the org shares that allowance. The client tracks usage **at no extra cost**:
each REST response carries a `Sforce-Limit-Info: api-usage=used/max` header, and the client
reads it.

| Source | Gives | Cost |
|---|---|---|
| `Sforce-Limit-Info` header on every response | org-wide daily API calls used / max (and `api-bursts` when present) | free |
| library counters | calls by operation, sObject and caller; errors by code; retries; refreshes | free |
| `sf.limits()` | every org limit (API, storage, Bulk, e-mail, ...) | 1 API call |
| `sf.start_limits_poller()` | `sf.limits()` on a timer, at least 5 minutes apart | 1 API call per poll |

## `sf.usage.snapshot()`

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

| `UsageSnapshot` field | Meaning |
|---|---|
| `api_used`, `api_max`, `api_fraction` | from the latest `Sforce-Limit-Info` header: **org-wide**, all integrations |
| `api_bursts_used`, `api_bursts_max` | `api-bursts` from the same header, when Salesforce sends it |
| `as_of` | when that header was seen (UTC) |
| `calls_by_operation`, `calls_by_sobject`, `calls_by_caller` | calls made by **this client family in this process** (`"-"` = no caller tag) |
| `errors_by_code` | failed calls by `error_code`, or by exception class name when there is no code |
| `retries`, `refreshes` | retries made, and token refreshes after a rejected or expired token |
| `projected_exhaustion` | when the org will hit `api_max` at the current rate, or `None` |

The projection is linear and uses the header values seen in the last hour. It is `None` until
at least two samples one minute apart show usage growing. Usage values are `None` until the
first response arrives. Describe calls served from the cache, and calls that the budget guard
blocks, send nothing, so they don't change the header values.

## Caller tags and priority

Tag calls so you can see where your usage goes, and so the budget guard can tell important
calls from deferrable ones:

```python
backfill = sf.tagged(caller="job:backfill", priority="low")
await backfill.query("SELECT Id FROM Lead LIMIT 10")

with esf.call_context(caller="route:/leads", priority="high"):
    await sf.query("SELECT Id FROM Lead LIMIT 10")  # tagged too, across awaits

print(sf.usage.snapshot().calls_by_caller)  # {'-': 2, 'job:backfill': 1, 'route:/leads': 1}
```

- `sf.tagged(caller=..., priority=...)` returns a child view. It shares everything with `sf`
  and adds the tags to each call.
- `esf.call_context(caller=..., priority=...)` sets the tags for every call made inside the
  block, in any client. It uses a context variable, so it works across `await`, and
  Collections threads in the sync client inherit it.
- Priority is `"low"`, `"normal"` (the default) or `"high"`. The precedence is: the view's tags
  (`tagged`), then the context (`call_context`), then the default.
- Tags appear in `sf.request` events as `caller` and `priority`.

To show the integration's name to **Salesforce admins**, pass `client_name="acme-sync"` to the
client. It is sent as `Sforce-Call-Options: client=acme-sync` on every API call. The name may
contain letters, digits and `_ . : -`, and be up to 64 characters. If you set your own
`Sforce-Call-Options` header on a call, the two are merged.

!!! note "Unverified"
    Where admins see the `client` value has not been checked against a live org yet
    (verification item V20 in the spec).

## Budgets

`budget=esf.Budget(...)` turns on threshold events and a guard. Both use the latest header
value. Neither makes extra calls.

```python
budget = esf.Budget(
    warn_at=(0.5, 0.8, 0.95),  # emit sf.usage.threshold once per threshold per UTC day
    pause_low_priority_above=0.8,  # BudgetExceeded for priority="low" calls above 80 %
    block_all_above=0.98,  # BudgetExceeded for everything except priority="high"
)
guarded = esf.AsyncSalesforce(auth, budget=budget)
await guarded.aclose()
```

| Field | Default | Effect |
|---|---|---|
| `warn_at` | `(0.5, 0.8, 0.95)` | when the fraction reaches a threshold, emit `sf.usage.threshold` (`threshold`, `fraction`, `used`, `max`), at most once per threshold per UTC day |
| `pause_low_priority_above` | `0.8` | above this fraction, `priority="low"` calls raise `esf.BudgetExceeded` **before sending** |
| `block_all_above` | `None` | above this fraction, every call except `priority="high"` raises `BudgetExceeded` |

`BudgetExceeded` is a `LimitError` with `error_code="BUDGET_EXCEEDED"`. Its `.usage` holds the
snapshot, so a job can sleep and resume:

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

Without `budget=`, there are no threshold events and no guard. Usage is still tracked.

## `sf.limits()`

```python
limits = await sf.limits()
daily = limits["DailyApiRequests"]
print(daily.max, daily.remaining, daily.used)
print(sorted(limits)[:3])
```

`limits()` costs one API call and is never called implicitly. It returns
`dict[str, esf.Limit]`, where `Limit` has `max`, `remaining`, `used` and `details` (any other
keys in the entry, such as per-app breakdowns). It also updates the usage tracker from
`DailyApiRequests`.

To refresh the numbers while the process is idle, start the opt-in poller. The interval must be
at least 300 seconds. There is one poller per client family, and it stops when the root client
is closed. With `AsyncSalesforce`, call it from inside a running event loop. `Salesforce`
uses a daemon thread.

```python
sf.start_limits_poller(interval=900)
await sf.aclose()  # stops the poller
```
