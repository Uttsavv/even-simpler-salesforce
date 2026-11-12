# Tutorial

In this tutorial, you build a small leads API with FastAPI and even-simpler-salesforce. It takes
about 25 minutes to code along, and less to read.

When you finish, your API can:

- capture new leads;
- find leads and page through them;
- update one lead or many leads at once;
- convert a qualified lead into an account, a contact and an opportunity;
- log the Salesforce calls of each request;
- run its tests without an org.

👉 **A step like this one is something you do.** The text between the steps explains what
happened.

## Before you start

You need:

- Python 3.10 or newer;
- a Salesforce org where you can create and convert leads, such as a Developer Edition org or a
  sandbox;
- an External Client App that allows the client credentials flow.

If you don't have the app yet, follow [Salesforce setup](authentication/salesforce-setup.md) and
[Client credentials](authentication/client-credentials.md). Come back when you have a client id
and a client secret. If you use a different login flow, [Authentication](authentication/index.md)
shows how to create it. The rest of the tutorial stays the same.

!!! warning
    Don't code along against a production org. The app creates, changes and converts real leads.

👉 **Create a project and install the packages**

```sh
mkdir leads-api && cd leads-api
python -m venv .venv && source .venv/bin/activate
pip install --pre "even-simpler-salesforce[fastapi]" uvicorn
```

👉 **Put the credentials of your app in the environment**

```sh
export SF_DOMAIN=https://acme.my.salesforce.com
export SF_CLIENT_ID=3MVG9...
export SF_CLIENT_SECRET=...
```

## Create the app

👉 **Create `main.py`**

```python
# main.py
import os
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from pydantic import BaseModel

import even_simpler_salesforce as esf
from even_simpler_salesforce.contrib.fastapi import SalesforceIntegration

auth = esf.ClientCredentials(
    domain=os.environ["SF_DOMAIN"],
    client_id=os.environ["SF_CLIENT_ID"],
    client_secret=os.environ["SF_CLIENT_SECRET"],
)
esf_app = SalesforceIntegration(auth)
app = FastAPI(title="Leads API", lifespan=esf_app.lifespan)
esf_app.init_app(app)

SF = Annotated[esf.AsyncSalesforce, Depends(esf_app.admin)]


@app.get("/me")
async def me(sf: SF) -> dict[str, Any]:
    info = await sf.auth.userinfo()
    return {"username": info["preferred_username"]}
```

`auth` is the login flow. It holds your credentials, but it sends nothing. `SalesforceIntegration`
creates one `AsyncSalesforce` client when the app starts and closes it when the app stops. The `SF`
type gives that client to any route that asks for it.

👉 **Start the server**

```sh
uvicorn main:app --reload
```

👉 **Call your first route**

```sh
curl localhost:8000/me
```

```json
{"username": "integration@acme.example"}
```

That is the user your app acts as. The first call got a token, and the client keeps it in
memory. When the token expires, the client gets a new one and sends the call again. You don't
write any code for that.

FastAPI also lists your routes at <http://localhost:8000/docs>. You can try each route from that
page instead of using `curl`.

## Capture a lead

A lead needs a last name and a company. A Pydantic model checks that before you call
Salesforce.

👉 **Add a model and a route to `main.py`**

```python
class LeadIn(BaseModel):
    LastName: str
    Company: str
    FirstName: str | None = None
    Email: str | None = None


@app.post("/leads", status_code=201)
async def create_lead(lead: LeadIn, sf: SF) -> dict[str, str]:
    lead_id = await sf.sobject("Lead").create(lead.model_dump(exclude_none=True))
    return {"id": lead_id}
```

👉 **Create a lead**

```sh
curl -X POST localhost:8000/leads -H 'Content-Type: application/json' \
  -d '{"FirstName": "Ada", "LastName": "Lovelace", "Company": "Analytical Engines"}'
```

```json
{"id": "00Q5g00000KpLmQEAV"}
```

`sf.sobject("Lead")` gives you the operations for one object. `create()` returns the id of the
new record. Keep the id: you use it in the next steps.

## Read a lead

👉 **Add a route that gets one lead**

```python
LEAD_FIELDS = ["Id", "FirstName", "LastName", "Company", "Email", "Status"]


def clean(record: dict[str, Any]) -> dict[str, Any]:
    # Drop the Salesforce metadata that your callers don't need.
    return {k: v for k, v in record.items() if k != "attributes"}


@app.get("/leads/{lead_id}")
async def get_lead(lead_id: str, sf: SF) -> dict[str, Any]:
    return clean(await sf.sobject("Lead").get(lead_id, fields=LEAD_FIELDS))
```

👉 **Get the lead you created**

```sh
curl localhost:8000/leads/00Q5g00000KpLmQEAV
```

```json
{"Id": "00Q5g00000KpLmQEAV", "FirstName": "Ada", "LastName": "Lovelace", "Company": "Analytical Engines", "Email": null, "Status": "Open - Not Contacted"}
```

The lead is a plain `dict` with the fields you asked for. Salesforce filled in `Status` with the
default value of your org.

👉 **Now ask for a lead that doesn't exist**

```sh
curl -i localhost:8000/leads/00Q000000000000AAA
```

```text
HTTP/1.1 500 Internal Server Error
```

Your terminal shows the reason: `even_simpler_salesforce.errors.NotFound`. The client raises a
typed error for each kind of failure. Your API should answer `404`, not `500`.

## Turn errors into responses

👉 **Turn on the error handlers**

```diff
-esf_app = SalesforceIntegration(auth)
+esf_app = SalesforceIntegration(auth, error_handlers=True)
```

👉 **Ask for the missing lead again**

```sh
curl -i localhost:8000/leads/00Q000000000000AAA
```

```text
HTTP/1.1 404 Not Found

{"code":"salesforce_not_found","error_code":"NOT_FOUND"}
```

The integration maps each typed error to an HTTP status and a JSON body. A rejected record becomes
`422`, and a hit API limit becomes `503` with `Retry-After`. You see a `422` in
[Update a lead](#update-a-lead). The [FastAPI](integrations/fastapi.md#map-errors-to-http-responses)
page lists every mapping.

## Find leads

👉 **Add a route that finds leads by status**

```python
@app.get("/leads")
async def list_leads(sf: SF, status: str = "Open - Not Contacted") -> list[dict[str, Any]]:
    page = await sf.query(
        "SELECT Id, Name, Company, Status FROM Lead WHERE Status = {} ORDER BY CreatedDate DESC",
        status,
    )
    return [clean(r) for r in page.records]
```

👉 **Find the open leads**

```sh
curl localhost:8000/leads
```

```json
[{"Id": "00Q5g00000KpLmQEAV", "Name": "Ada Lovelace", "Company": "Analytical Engines", "Status": "Open - Not Contacted"}]
```

Each `{}` in the query takes the next argument. The client quotes and escapes the value for you.
If a caller sends `status=x' OR Name != '`, the query still looks for that exact status. Never
put a caller's value into SOQL with an f-string.

## Page through leads

`sf.query()` returns one page of up to 2,000 rows. If there are more rows, the page has a
`cursor` that you can pass back to get the next page. Your API can give the cursor to its own
callers.

A cursor that leaves your server can come back changed. Give the client a key, and it signs each
cursor and rejects one that a caller changed.

👉 **Stop the server, create a key, and start the server again**

```sh
export SF_CURSOR_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn main:app --reload
```

Use the same key on every worker of your app. Keep it as secret as your client secret.

👉 **Create the client yourself, with the key**

```diff
-esf_app = SalesforceIntegration(auth, error_handlers=True)
+esf_app = SalesforceIntegration(
+    client=esf.AsyncSalesforce(auth, cursor_key=os.environ["SF_CURSOR_KEY"].encode()),
+    error_handlers=True,
+)
```

👉 **Change `list_leads` to accept a cursor and return one**

```diff
 @app.get("/leads")
-async def list_leads(sf: SF, status: str = "Open - Not Contacted") -> list[dict[str, Any]]:
-    page = await sf.query(
-        "SELECT Id, Name, Company, Status FROM Lead WHERE Status = {} ORDER BY CreatedDate DESC",
-        status,
-    )
-    return [clean(r) for r in page.records]
+async def list_leads(
+    sf: SF, status: str = "Open - Not Contacted", cursor: str | None = None
+) -> dict[str, Any]:
+    if cursor:
+        page = await sf.query_more(cursor)
+    else:
+        page = await sf.query(
+            "SELECT Id, Name, Company, Status FROM Lead WHERE Status = {} ORDER BY CreatedDate DESC",
+            status,
+            page_size=200,
+        )
+    return {"records": [clean(r) for r in page.records], "next_cursor": page.cursor}
```

👉 **Get the first page, then the next one if `next_cursor` isn't `null`**

```sh
curl localhost:8000/leads
curl "localhost:8000/leads?cursor=<the next_cursor of the first page>"
```

`page_size=200` asks for the smallest page that Salesforce allows. If your org has 200 matching
leads or fewer, the first page is also the last one. Then `next_cursor` is `null`, and there is no
second request to make. The same code pages through the leads when your org has more of them.

If a caller sends a changed cursor, your API answers `400` with the code
`salesforce_invalid_cursor`.

## Update a lead

👉 **Add a model with optional fields and a route that updates a lead**

```python
from fastapi import Response


class LeadPatch(BaseModel):
    FirstName: str | None = None
    LastName: str | None = None
    Company: str | None = None
    Email: str | None = None
    Status: str | None = None


@app.patch("/leads/{lead_id}", status_code=204)
async def update_lead(lead_id: str, changes: LeadPatch, sf: SF) -> Response:
    await sf.sobject("Lead").update(lead_id, changes.model_dump(exclude_unset=True))
    return Response(status_code=204)
```

`exclude_unset=True` keeps only the fields the caller sent. Salesforce changes those fields and
leaves the others alone.

👉 **Give the lead an email address**

```sh
curl -X PATCH localhost:8000/leads/00Q5g00000KpLmQEAV \
  -H 'Content-Type: application/json' -d '{"Email": "ada@example.com"}'
```

The route answers `204 No Content`. Get the lead again to see the new email address.

👉 **Now try to clear the last name**

```sh
curl -X PATCH localhost:8000/leads/00Q5g00000KpLmQEAV \
  -H 'Content-Type: application/json' -d '{"LastName": null}'
```

```json
{"code": "salesforce_record_error", "error_code": "REQUIRED_FIELD_MISSING", "message": "Required fields are missing: [LastName]", "fields": ["LastName"]}
```

Salesforce rejected the change, and your API answered `422` with the reason. Validation rules,
duplicate rules and triggers in your org fail the same way, each with its own `error_code`.

## Update many leads

To change many records, send them in one call to `sf.collections`. The client splits the rows into
chunks of 200 and sends the chunks concurrently.

👉 **Add a route that updates many leads**

```python
class LeadChange(LeadPatch):
    Id: str


@app.patch("/leads")
async def update_leads(changes: list[LeadChange], sf: SF) -> dict[str, Any]:
    rows = [change.model_dump(exclude_unset=True) for change in changes]
    try:
        results = await sf.collections.update("Lead", rows)
    except esf.PartialFailure as exc:
        results = exc.results  # a whole chunk failed, but the other chunks were saved
    return {
        "updated": [r.id for r in results.succeeded],
        "failed": [
            {"id": row["Id"], "error": r.errors[0].message} for row, r in zip(rows, results) if not r.ok
        ],
    }
```

👉 **Create a second lead**

```sh
curl -X POST localhost:8000/leads -H 'Content-Type: application/json' \
  -d '{"FirstName": "Grace", "LastName": "Hopper", "Company": "Navy"}'
```

```json
{"id": "00Q5g00000KpMnREAV"}
```

👉 **Update both leads, with one bad email address**

```sh
curl -X PATCH localhost:8000/leads -H 'Content-Type: application/json' -d '[
  {"Id": "00Q5g00000KpLmQEAV", "Status": "Working - Contacted"},
  {"Id": "00Q5g00000KpMnREAV", "Email": "grace-at-navy"}
]'
```

```json
{"updated": ["00Q5g00000KpLmQEAV"], "failed": [{"id": "00Q5g00000KpMnREAV", "error": "Email: invalid email address: grace-at-navy"}]}
```

One row failed, and Salesforce still saved the other row. The client doesn't raise for a failed
row. It returns one result per row, in the order you sent them. If you want an error instead,
call `results.raise_for_errors()`.

A whole chunk can also fail, for example after a timeout. Then the client raises
`esf.PartialFailure`, and its `results` still holds one result per row. The route catches it, so
the caller learns which rows Salesforce saved. [Collections](guides/collections.md#handle-a-failed-chunk)
explains the outcomes.

## Convert a lead

Ada Lovelace wants to buy. Convert the lead, and Salesforce creates an account, a contact and an
opportunity from it.

👉 **Add a route that converts a lead**

```python
@app.post("/leads/{lead_id}/convert")
async def convert_lead(lead_id: str, sf: SF) -> dict[str, Any]:
    result = await sf.leads.convert(lead_id, converted_status="Closed - Converted")
    return {
        "account_id": result.account_id,
        "contact_id": result.contact_id,
        "opportunity_id": result.opportunity_id,
    }
```

`converted_status` must be a lead status of your org that has **Converted** checked. Find the
values under **Setup > Lead Status**.

👉 **Convert the lead**

```sh
curl -X POST localhost:8000/leads/00Q5g00000KpLmQEAV/convert
```

```json
{"account_id": "0015g00000QxAbCAAV", "contact_id": "0035g00000RyDeFAAV", "opportunity_id": "0065g00000SzGhIAAV"}
```

The REST API can't convert leads, so the client uses the SOAP API for this one call. It sends the
same token, and you don't write any SOAP.

## See the Salesforce calls

Each request to your API makes Salesforce calls. Each call takes time and uses your daily API
limit. The integration can tell you how many calls each request made.

👉 **Add a function that logs the Salesforce calls of a request**

Put it above `esf_app = SalesforceIntegration(...)`, because the integration needs the function
when Python creates it.

```python
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("leads")


def log_summary(request, fields: dict[str, Any]) -> None:
    log.info(
        "%s %r sf_calls=%s sf_ms=%s", request.method, request.url.path, fields["sf_calls"], fields["sf_ms"]
    )
```

👉 **Give it to the integration**

```diff
 esf_app = SalesforceIntegration(
     client=esf.AsyncSalesforce(auth, cursor_key=os.environ["SF_CURSOR_KEY"].encode()),
     error_handlers=True,
+    on_summary=log_summary,
 )
```

👉 **Call a few routes and watch your terminal**

```text
INFO:leads:GET '/leads' sf_calls=1 sf_ms=212
INFO:leads:PATCH '/leads' sf_calls=1 sf_ms=187
```

`fields` holds more than these two values, such as `sf_errors` and `sf_api_used`. Send them to
your log platform to find the slow routes and the routes that use the most API calls.
[Logging](logging.md) lists every field.

## Test your API

Your tests don't need an org. The `esf_async_fake` fixture gives you an in-memory org with the same
methods as `AsyncSalesforce`.

👉 **Install pytest**

```sh
pip install pytest httpx
```

👉 **Create `test_main.py`**

<!-- esf: skip -->
```python
# test_main.py
from fastapi.testclient import TestClient

from main import app, esf_app


def test_capture_and_convert_a_lead(esf_async_fake) -> None:
    app.dependency_overrides[esf_app.admin] = lambda: esf_async_fake

    with TestClient(app) as client:
        created = client.post("/leads", json={"LastName": "Lovelace", "Company": "Engines"})
        lead_id = created.json()["id"]
        converted = client.post(f"/leads/{lead_id}/convert")

    assert created.status_code == 201
    assert converted.json()["account_id"] is not None
    app.dependency_overrides.clear()
```

👉 **Run the tests**

```sh
pytest
```

Run them in the shell where you set the `SF_*` variables, because `main.py` reads them when
pytest imports it.

The routes ran against the fake, and no request left your machine. You can also seed records,
script query results and inject errors. [Testing](testing.md) shows how.

## Recap

You built an API that:

- gets a token and refreshes it with one login flow;
- creates, reads and updates leads with `sf.sobject("Lead")`;
- finds leads with safe query values and pages through them with signed cursors;
- updates many leads with `sf.collections` and reports each failed row;
- converts a lead with `sf.leads.convert()`;
- turns Salesforce errors into HTTP responses;
- logs the Salesforce calls of each request;
- runs its tests against an in-memory org.

A larger version of this app is in
[`examples/fastapi_app`](https://github.com/Uttsavv/even-simpler-salesforce/tree/main/examples/fastapi_app).

## Next steps

- Act as each user who logs in, with the [web server flow](authentication/web-server-flow.md).
- Load thousands of records with [Bulk API 2.0](guides/bulk.md).
- Stop a runaway job before it uses your daily limit, with [usage and budgets](usage-and-budgets.md).
- Learn the rest of the records API in [Records](guides/records.md).
