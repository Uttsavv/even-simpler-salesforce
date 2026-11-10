# Apex REST

`sf.apex` calls your own Apex REST classes under `/services/apexrest`. Use it for endpoints
that a `@RestResource` class in the org exposes.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

order = await sf.apex.post("/orders/submit", {"orderId": "801xx0000000001"})
print(order["status"])  # 'Submitted'
```

Pass the path after `/services/apexrest`, starting with `/`. The call returns what the class
returned: decoded JSON, a `str` for a text body, `bytes` for a binary body, or `None` for an
empty body.

## Call a method

There is one method per HTTP method: `get`, `post`, `put`, `patch` and `delete`. Pass query
parameters with `params=` and a JSON body as the second argument.

```python
order = await sf.apex.get("/orders/801xx0000000001", params={"fields": "status"})
await sf.apex.patch("/orders/801xx0000000001", {"status": "Cancelled"})
assert await sf.apex.delete("/orders/801xx0000000001") is None
```

You get the same pipeline as every other call: the token, one refresh on `401`, typed errors,
usage tracking and one `sf.request` event. The event has the operation `apex.get`,
`apex.post` and so on.

## Handle errors

If the class returns an error status, the call raises `esf.ApexError`. `.status` holds the HTTP
status and `.body` holds the decoded body that the class returned.

```python
try:
    await sf.apex.post("/orders/closed", {"orderId": "801xx0000000002"})
except esf.ApexError as exc:
    print(exc.status, exc.body["error"])  # 422 ORDER_CLOSED
```

An unhandled exception in the class also raises `ApexError`, with `.error_code` set to
`APEX_ERROR` and the exception text in `.message`. A path that no class maps raises
`ApexError` with `.error_code` set to `NOT_FOUND`.

These errors keep their own class, because the cause is not the Apex class. A `401` or `429`
that your class sets itself, with its own body, raises `ApexError` like any other status:

| Error | Raised for |
|---|---|
| `SessionExpired` | `INVALID_SESSION_ID` that remains after one token refresh |
| `RequestLimitExceeded` | `REQUEST_LIMIT_EXCEEDED` |
| `ServerError` | `SERVER_UNAVAILABLE`, or a 5xx with an empty body |
| `UnexpectedResponse` | an error page that is not JSON, for example from a proxy |

## Resend a safe call

Apex REST runs code, so the client never resends a call after it may have reached Salesforce.
This also applies to `GET`. If you know that a method is safe to run twice, pass
`idempotent=True`. Then the client retries it after a timeout or a 5xx.

```python
order = await sf.apex.get("/orders/801xx0000000001", idempotent=True)
```

## Test code that calls Apex REST

`FakeSalesforce` answers Apex REST calls with handlers that you register. See
[Testing](../testing.md#fake-actions-and-apex-rest).
