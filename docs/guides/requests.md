# Raw requests

`sf.request()` calls any REST resource. Use it for resources the library doesn't wrap yet.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

counts = await sf.request("GET", "limits/recordCount", params={"sObjects": "Lead,Account"})
print(counts["sObjects"])
```

A raw request goes through the same pipeline as every other method. You get these for free:

- the token, and one refresh on `401`
- the budget guard and the concurrency limit
- retries and typed errors
- usage tracking and one `sf.request` event

## Send a body

Pass a JSON body with `json=`. The client encodes it with its JSON codec.

```python
score = await sf.request("POST", "/services/apexrest/LeadScore", json={"email": "ada@example.com"})
print(score)

lead_id = (await sf.query("SELECT Id FROM Lead LIMIT 1")).records[0]["Id"]
assert await sf.request("PATCH", f"sobjects/Lead/{lead_id}", json={"Rating": "Hot"}) is None
```

To send raw bytes, pass `content=`. The default `Content-Type` is `application/octet-stream`.
To send another type, set `Content-Type` in `headers=`.

Pass `json=` or `content=`, not both. `GET` and `HEAD` can't have a body. Both mistakes raise
`ValueError`.

## Read the response

The return value depends on the response body:

| Body | Return value |
|---|---|
| JSON | the decoded JSON |
| empty, for example `204` | `None` |
| text: `text/*`, XML, JavaScript, CSV, form data, `+json` or `+xml` types, or any `charset=` | the text |
| no `Content-Type`, and the body is valid UTF-8 | the text |
| anything else, for example file contents from `sobjects/ContentVersion/{id}/VersionData` | the raw `bytes` |

Every 2xx status counts as success. The client never sends `X-PrettyPrint`.

## Choose a path

A relative path starts after `/services/data/vXX.X/`. An absolute path starts with
`/services/`, and the client uses it as given under the instance URL.

| `path` | URL |
|---|---|
| `"sobjects/Lead/describe"` | `{instance}/services/data/v{api_version}/sobjects/Lead/describe` |
| `"/services/apexrest/LeadScore"` | `{instance}/services/apexrest/LeadScore` |

The client raises `ValueError` for these paths:

- absolute URLs such as `https://...`
- paths outside `/services/`
- paths with `..` or `.` segments

So a path built from user input can't reach another host.

## Retry a write safely {#retries-and-idempotent}

If a request may have reached Salesforce, the client resends it only if it is a read. This
covers a timeout or reset after the send, and a 5xx response.
[ADR 0001](../adr/0001-retry-safety.md) sets this rule.

| Request | Inferred `idempotent` | Resent after a timeout or 5xx |
|---|---|---|
| `GET`, `HEAD` | `True` | yes |
| `GET /services/apexrest/...`, `GET tooling/executeAnonymous` | `False`, because they run code | no |
| `POST`, `PATCH`, `PUT`, `DELETE` | `False` | no |

To override the inferred value, pass `idempotent=True` or `idempotent=False`.

!!! warning "Pass `idempotent=True` only if a repeat is harmless"
    If the request runs triggers, flows or e-mails, a retry can run them twice. If a repeat
    gives a different result, a retry can change your data. Leave `idempotent` unset for such
    requests.

Some reads use POST and are safe to repeat:

```python
found = await sf.request(
    "POST",
    "parameterizedSearch",
    json={"q": "Lovelace", "sobjects": [{"name": "Lead"}]},
    idempotent=True,
    operation="parameterized_search",
)
print(len(found["searchRecords"]))
```

Some failures mean the request didn't run. The client retries these for every method, whatever
`idempotent` says:

- connect errors, because the client never sent the request
- `UNABLE_TO_LOCK_ROW` (`RowLocked`)
- `429` or `REQUEST_LIMIT_EXCEEDED`, if Salesforce sends `Retry-After`

See [Errors → Retries](../errors.md#retries) for the policy settings.

## Tokens in the request body

Some APIs take the access token in the body, not in the `Authorization` header. An example is
the SOAP `SessionHeader`. If Salesforce rejects that token, the client can't refresh it for
you, because the fault arrives in the body. Catch the fault, call `sf.auth.refresh()` and send
the request again once.

```python
from xml.sax.saxutils import escape

SOAP_PATH = "/services/Soap/u/68.0"


def user_info_envelope(session_id: str) -> bytes:
    return (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:urn="urn:partner.soap.sforce.com"><soapenv:Header><urn:SessionHeader>'
        f"<urn:sessionId>{escape(session_id)}</urn:sessionId></urn:SessionHeader></soapenv:Header>"
        "<soapenv:Body><urn:getUserInfo/></soapenv:Body></soapenv:Envelope>"
    ).encode()


async def soap_user_info(token: esf.Token) -> str:
    return await sf.request(
        "POST",
        SOAP_PATH,
        content=user_info_envelope(token.access_token),
        headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""'},
        operation="soap.get_user_info",
    )


try:
    xml = await soap_user_info(await sf.auth.token())
except esf.UnexpectedResponse as exc:  # a SOAP fault is HTTP 500 with an XML body
    if b"INVALID_SESSION_ID" not in (exc.raw or b""):
        raise
    xml = await soap_user_info(await sf.auth.refresh())
print("<userId>" in xml)  # True
```

Concurrent calls to `sf.auth.refresh()` share one refresh. See
[Force a refresh](../authentication/index.md#force-a-refresh).

## Name the operation

`operation=` names the call. The default is `"raw"`. The name appears in these places:

- `sf.request` events
- `sf.usage.snapshot().calls_by_operation`
- error objects, as `exc.operation`
- `FakeSalesforce.fail_next()`

Choose stable names with few distinct values, such as `"apex.lead_score"`. Don't put ids or
user input in them.

```python
await sf.request("GET", "sobjects/Account/describe", operation="account.describe")
print(sf.usage.snapshot().calls_by_operation)
await sf.aclose()
```

## Arguments

| Argument | Meaning |
|---|---|
| `method` | `"GET"`, `"POST"`, `"PATCH"`, `"PUT"`, `"DELETE"`, `"HEAD"` |
| `path` | relative to `/services/data/vXX.X/`, or absolute under `/services/` |
| `params=` | query-string parameters |
| `json=` | body, encoded with the JSON codec of the client |
| `content=` | raw body bytes (default `Content-Type: application/octet-stream`) |
| `headers=` | extra headers; a `Content-Type` here, in any letter case, wins |
| `idempotent=` | override the retry-safety rule (see [above](#retries-and-idempotent)) |
| `operation=` | operation name for events, usage counters and errors (default `"raw"`) |
