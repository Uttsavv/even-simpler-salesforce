# Records

`sf.sobject(name)` returns an object for one sObject type. It is cheap to create, so make one
wherever you need it. The examples use the async client; with `esf.Salesforce`, drop `await`.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)
leads = sf.sobject("Lead")
```

| Method | REST call | Returns |
|---|---|---|
| `get(id, fields=None, *, model=None, if_modified_since=None)` | `GET sobjects/{o}/{id}` | the record (`None` on `304`) |
| `get_by_external_id(field, value, fields=None, *, model=None)` | `GET sobjects/{o}/{field}/{value}` | the record |
| `create(data, *, assignment_rule_id=None, allow_save=None)` | `POST sobjects/{o}` | the new id (`str`) |
| `update(id, data, *, assignment_rule_id=None, allow_save=None, if_unmodified_since=None)` | `PATCH sobjects/{o}/{id}` | `None` |
| `upsert(field, value, data, *, assignment_rule_id=None, allow_save=None)` | `PATCH sobjects/{o}/{field}/{value}` | `UpsertResult(id, created)` |
| `delete(id, *, if_unmodified_since=None)` | `DELETE sobjects/{o}/{id}` | `None` |
| `describe(*, cached=True)` | `GET sobjects/{o}/describe` | describe `dict` (cached) |
| `metadata()` | `GET sobjects/{o}` | basic info and recent items |
| `updated(start, end)` / `deleted(start, end)` | `GET sobjects/{o}/updated/` / `deleted/` | ids changed or deleted in the window |
| `sf.describe_global(*, cached=True)` | `GET sobjects` | list of sObjects (cached) |

The library validates sObject names (`^[A-Za-z][A-Za-z0-9_]*$`), record ids (15 or 18
alphanumeric characters) and field names before it builds a URL. A bad value raises
`ValueError` and nothing is sent.

## Create, read, update, delete

```python
lead_id = await leads.create({"FirstName": "Ada", "LastName": "Lovelace", "Company": "Engines"})

lead = await leads.get(lead_id)                                    # every field
small = await leads.get(lead_id, fields=["Id", "Name", "Status"])  # just these
print(small["Name"], small["Status"])

await leads.update(lead_id, {"Status": "Working", "Rating": "Hot"})
await leads.delete(lead_id)
```

Records are plain `dict`s, exactly as Salesforce returns them, including the `attributes` key.
Dates and datetimes stay strings unless you use a [model](#typed-records-model). `create`
returns only the id. Salesforce returns nothing else that is useful.

Values are JSON-encoded with these rules (the same with orjson and the stdlib):

- `Decimal` is sent as an exact JSON number, never through `float`;
- NaN and ±Infinity raise `ValueError` before anything is sent, so a field is never blanked by
  accident;
- `datetime` and `date` become ISO 8601 strings, and `set`, `frozenset` and `tuple` become
  arrays.

```python
from decimal import Decimal

opp_id = await sf.sobject("Opportunity").create(
    {"Name": "Renewal", "StageName": "Prospecting", "CloseDate": "2027-01-31",
     "Amount": Decimal("12500.10")}
)
```

## External ids and upsert

```python
lead = await leads.get_by_external_id("External_Id__c", "ext-hopper", fields=["Id", "Name"])

result = await leads.upsert("External_Id__c", "crm-1001", {"LastName": "Liskov", "Company": "MIT"})
print(result.created, result.id)   # True, '00Q...' on the first run; False on later runs
```

The value is percent-encoded as **one path segment**: a space becomes `%20`, `/` becomes `%2F`
and `+` becomes `%2B`. So values with slashes or spaces reach Salesforce unchanged.

If the external id matches more than one record, Salesforce answers `300 Multiple Choices`
and the call raises `esf.MultipleMatches`. Its `.matches` attribute holds the URLs of the
matching records.

!!! warning "Upserts are not retried after a timeout"
    If an upsert times out after it was sent, it may already have created the record. Sending
    it again would report `created=False` and run triggers a second time. So the client raises
    `RequestTimeout` and leaves the decision to you (see [Retries](../errors.md#retries)).

## Request headers as keyword arguments

| Keyword | Header | Effect |
|---|---|---|
| `assignment_rule_id="01Q..."` | `Sforce-Auto-Assign: 01Q...` | run that assignment rule |
| `assignment_rule_id=True` / `False` | `Sforce-Auto-Assign: TRUE` / `FALSE` | run the default rule, or none |
| `allow_save=True` / `False` | `Sforce-Duplicate-Rule-Header: allowSave=...` | save despite a duplicate-rule *alert* (or not) |
| `if_modified_since=dt` (get) | `If-Modified-Since` | returns `None` when the record hasn't changed (`304`) |
| `if_unmodified_since=dt` (update, delete) | `If-Unmodified-Since` | `esf.Conflict` (`412`) if the record changed after `dt` |

Datetimes must be timezone-aware. A naive `datetime` raises `ValueError`.

```python
from datetime import datetime, timedelta, timezone

since = datetime.now(timezone.utc) - timedelta(minutes=5)
lead_id = await leads.create({"LastName": "Hamilton", "Company": "NASA"}, assignment_rule_id=True)
fresh = await leads.get(lead_id, if_modified_since=since)   # None if unchanged since `since`

try:
    await leads.update(lead_id, {"Status": "Working"}, if_unmodified_since=since)
except esf.Conflict:
    print("someone else changed the lead; reload and try again")
```

## Typed records (`model=`)

`get`, `get_by_external_id`, `query`, `query_more` and `query_iter` take `model=`:

- a **Pydantic** model (anything with `model_validate`) receives the whole record, including
  `attributes`, which Pydantic ignores by default;
- any other callable, such as a **dataclass**, is called with the record's fields as keyword
  arguments, without `attributes`. A dataclass must therefore declare every field the query
  returns.

```python
from dataclasses import dataclass

from pydantic import BaseModel, Field


class LeadRow(BaseModel):
    id: str = Field(alias="Id")
    name: str = Field(alias="Name")
    status: str | None = Field(default=None, alias="Status")


@dataclass
class LeadName:
    Id: str
    Name: str


row = await leads.get(lead_id, fields=["Id", "Name", "Status"], model=LeadRow)
print(row.name, row.status)

page = await sf.query("SELECT Id, Name FROM Lead LIMIT 3", model=LeadName)
print(page.records[0].Name)
```

Install Pydantic with the `[pydantic]` extra. With Pydantic, `Decimal` and `datetime` fields
are the easiest way to get exact numbers and real datetimes back.

## Describe

```python
describe = await leads.describe()                 # served from the describe cache when fresh
names = [f["name"] for f in describe["fields"]]
everything = await sf.describe_global()
print(len(everything["sobjects"]), names[:3])

fresh = await leads.describe(cached=False)        # skip the cache
```

The describe cache is keyed by org, API version, sObject and a hash of the user id, because
field-level security differs by user. A cached entry is used for `ttl` seconds (default 1 hour)
without asking Salesforce. After that, the client revalidates it with `If-Modified-Since`. A
`304 Not Modified` keeps the cached copy and costs no download. Stale entries are kept for
revalidation for `stale_ttl` (default 24 hours). Configure this with
`describe_cache=esf.DescribeCacheConfig(...)`, or share describes across processes by passing
your own `cache=` (anything with `get`, `set(key, value, ttl)` and `delete`, holding plain JSON
data).

## Changed and deleted records

```python
from datetime import datetime, timedelta, timezone

end = datetime.now(timezone.utc)
start = end - timedelta(days=1)
changed = await leads.updated(start, end)    # {"ids": [...], "latestDateCovered": "..."}
removed = await leads.deleted(start, end)    # {"deletedRecords": [...], ...}
print(len(changed["ids"]), len(removed["deletedRecords"]))

await sf.aclose()
```

Datetimes must be timezone-aware. They are sent in UTC.

## Errors you will meet here

| Situation | Exception |
|---|---|
| record id doesn't exist, or was deleted | `esf.NotFound` (`error_code` `NOT_FOUND` / `ENTITY_IS_DELETED`) |
| sObject type doesn't exist (`describe`, `metadata`, `updated`, `deleted`) | `esf.InvalidType` |
| unknown field | `esf.InvalidField` |
| validation rule, Flow custom error, `addError()` | `esf.ValidationRuleFailed` |
| required field missing | `esf.RequiredFieldMissing` |
| duplicate rule blocks the save | `esf.DuplicateDetected` (`.duplicates`) |
| record locked by another transaction | `esf.RowLocked` (retried automatically) |
| `if_unmodified_since` precondition failed | `esf.Conflict` |
| external id matched several records | `esf.MultipleMatches` |

See [Errors](../errors.md) for the whole hierarchy and for custom error classes.
