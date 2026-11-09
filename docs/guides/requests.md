# Raw requests

`sf.request()` calls any REST resource through the same pipeline as every other method: token,
refresh on `401`, budget guard, concurrency limit, retries, typed errors, usage tracking and one
`sf.request` event. Use it for resources the library doesn't wrap yet.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

counts = await sf.request("GET", "limits/recordCount", params={"sObjects": "Lead,Account"})
print(counts["sObjects"])
```

| Argument | Meaning |
|---|---|
| `method` | `"GET"`, `"POST"`, `"PATCH"`, `"PUT"`, `"DELETE"`, `"HEAD"` |
| `path` | relative to `/services/data/vXX.X/`, or absolute under `/services/` |
| `params=` | query-string parameters |
| `json=` | body, JSON-encoded with the client's codec |
| `content=` | raw body bytes (default `Content-Type: application/octet-stream`) |
| `headers=` | extra headers; a `Content-Type` given here (in any letter case) wins |
| `idempotent=` | override the retry-safety rule (see [below](#retries-and-idempotent)) |
| `operation=` | operation name for events, usage counters and errors (default `"raw"`) |

## Paths

- `"sobjects/Lead/describe"` is relative and becomes
  `{instance}/services/data/v{api_version}/sobjects/Lead/describe`;
- `"/services/apexrest/LeadScore"` is absolute, and is used as given under the instance URL;
- absolute URLs (`https://...`), paths outside `/services/`, and `..` / `.` segments raise
  `ValueError`, so a path built from user input can't reach another host.

## Bodies and return values

- Pass `json=` or `content=`, not both (`ValueError`).
- `GET` and `HEAD` can't have a body (`ValueError`).
- The return value is the decoded JSON for JSON responses, `None` for an empty body (e.g.
  `204`), and the text for any other body. Every 2xx status counts as success. `X-PrettyPrint`
  is never sent.

```python
score = await sf.request("POST", "/services/apexrest/LeadScore", json={"email": "ada@example.com"})
print(score)

lead_id = (await sf.query("SELECT Id FROM Lead LIMIT 1")).records[0]["Id"]
assert await sf.request("PATCH", f"sobjects/Lead/{lead_id}", json={"Rating": "Hot"}) is None
```

## Retries and `idempotent`

After a request **may have reached Salesforce** (a timeout or reset after sending, or a 5xx),
it is resent only if it is a read. This rule is set in
[ADR 0001](../adr/0001-retry-safety.md):

| Request | Inferred `idempotent` | Resent after timeout / 5xx? |
|---|---|---|
| `GET`, `HEAD` | `True` | yes |
| `GET /services/apexrest/...`, `GET tooling/executeAnonymous` | `False` (they run code) | no |
| `POST`, `PATCH`, `PUT`, `DELETE` | `False` | no |

`idempotent=True` or `idempotent=False` overrides the inferred value. Only pass `True` when
running the request twice is harmless: no triggers, flows or e-mails, and a repeat gives the
same result. Some reads are sent as POST and are safe to repeat:

```python
found = await sf.request(
    "POST", "parameterizedSearch", json={"q": "Lovelace", "sobjects": [{"name": "Lead"}]},
    idempotent=True, operation="parameterized_search",
)
print(len(found["searchRecords"]))
```

Some failures are retried **for every method**, whatever `idempotent` says, because the request
didn't run:

- connect errors (the request was never sent);
- `UNABLE_TO_LOCK_ROW` (`RowLocked`);
- `429` / `REQUEST_LIMIT_EXCEEDED` when Salesforce sends `Retry-After`.

See [Errors → Retries](../errors.md#retries) for the policy settings.

## Operation names

`operation=` names the call in `sf.request` events, in `sf.usage.snapshot().calls_by_operation`,
in error objects (`exc.operation`) and in `FakeSalesforce.fail_next()`. Choose stable,
low-cardinality names such as `"apex.lead_score"`. Don't put ids or user input in them.

```python
await sf.request("GET", "sobjects/Account/describe", operation="account.describe")
print(sf.usage.snapshot().calls_by_operation)
await sf.aclose()
```
