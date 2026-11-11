# Records

`sf.sobject(name)` gives you the methods to create, read, update and delete records of one
sObject type. Use it to work with one record at a time. To write many records per request, use
[Collections](collections.md).

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

leads = sf.sobject("Lead")
lead_id = await leads.create({"FirstName": "Ada", "LastName": "Lovelace", "Company": "Engines"})
lead = await leads.get(lead_id, fields=["Id", "Name", "Status"])
print(lead["Name"], lead["Status"])
```

`sf.sobject()` is cheap, so call it wherever you need it. The examples use the async client.
With `esf.Salesforce`, drop `await`.

## Create a record

Pass the field values as a `dict`. `create` returns the new record id as a `str`, because
Salesforce returns nothing else that is useful.

```python
lead_id = await leads.create({"FirstName": "Grace", "LastName": "Hopper", "Company": "Navy"})
print(lead_id)  # '00Q...'
```

## Get a record

Without `fields`, you get every field. With `fields`, you get only those.

```python
lead = await leads.get(lead_id)  # every field
small = await leads.get(lead_id, fields=["Id", "Name", "Status"])
print(small["Name"], small["Status"])
```

Records are plain `dict`s, as Salesforce returns them, including the `attributes` key. Dates and
datetimes stay strings unless you use a [model](#typed-records-model).

## Update a record

Pass only the fields you want to change. `update` returns `None`.

```python
await leads.update(lead_id, {"Status": "Working", "Rating": "Hot"})
```

## Delete a record

`delete` returns `None`. If the record doesn't exist, `delete` raises `esf.NotFound`.

```python
temp_id = await leads.create({"LastName": "Temp", "Company": "Acme"})
await leads.delete(temp_id)
```

## Upsert by external id

`upsert` updates the record whose external id field has the value you give, or creates it if
there is none. It returns `UpsertResult(id, created)`.

!!! warning "Upserts are not retried after a timeout"
    If an upsert times out after the client sends it, Salesforce may already have created the
    record. A second send reports `created=False` and runs triggers again. So the client raises
    `RequestTimeout` and leaves the decision to you. See [Retries](../errors.md#retries).

```python
result = await leads.upsert("External_Id__c", "crm-1001", {"LastName": "Liskov", "Company": "MIT"})
print(result.created, result.id)  # True, '00Q...' on the first run; False on later runs
```

Use `get_by_external_id` to read a record by its external id:

```python
lead = await leads.get_by_external_id("External_Id__c", "ext-hopper", fields=["Id", "Name"])
```

The value can contain slashes and spaces. It reaches Salesforce unchanged. If the value matches
more than one record, the call raises `esf.MultipleMatches`. Its `.matches` attribute holds the
URLs of the matching records.

## Typed records {#typed-records-model}

Pass `model=` to get objects instead of `dict`s. `get`, `get_by_external_id` and the query
methods that return rows take it.

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

The client treats any object with `model_validate` as a Pydantic model. A Pydantic model gets
the whole record, including `attributes`. Pydantic ignores `attributes` by default.

The client calls any other callable, such as a dataclass, with the fields of the record as
keyword arguments. It leaves out `attributes`. So a dataclass must declare every field that the
query returns.

Install Pydantic with the `[pydantic]` extra. Its `datetime` fields give you real datetimes
back. A `Decimal` field gives you a `Decimal`, but the client parses every JSON number as a
`float` first. A Number field can have up to
[18 digits](https://help.salesforce.com/s/articleView?id=000386249&type=1), more than a `float`
holds exactly. So a value with more than 15 significant digits can come back rounded. Values
that you send as `Decimal` stay exact.

## Write a model back

Send only the fields you changed. Make a model for the changes with every field optional. Then
dump it with `by_alias=True` and `exclude_unset=True`.

```python
class LeadChanges(BaseModel):
    status: str | None = Field(default=None, alias="Status")
    rating: str | None = Field(default=None, alias="Rating")


changes = LeadChanges(Status="Working")
body = changes.model_dump(by_alias=True, exclude_unset=True)
print(body)  # {'Status': 'Working'}
await leads.update(lead_id, body)
```

`by_alias=True` gives the Salesforce field names. `exclude_unset=True` leaves out the fields you
didn't set, so Salesforce keeps their values. Without it, the body has `"Rating": None`. The
client sends that as JSON `null`, which clears the field.

## Update only if unchanged

Pass `if_unmodified_since=` to `update` or `delete`. If the record changed after that time, the
call raises `esf.Conflict` and changes nothing. You can also pass `if_modified_since=` to `get`.
If the record hasn't changed since then, `get` returns `None`.

```python
from datetime import datetime, timedelta, timezone

since = datetime.now(timezone.utc) - timedelta(minutes=5)
fresh = await leads.get(lead_id, if_modified_since=since)  # None if unchanged since `since`

try:
    await leads.update(lead_id, {"Status": "Working"}, if_unmodified_since=since)
except esf.Conflict:
    print("someone else changed the lead; reload and try again")
```

Datetimes must be timezone-aware. A naive `datetime` raises `ValueError`.

## Assignment and duplicate rules

To run an assignment rule, pass `assignment_rule_id=` to `create`, `update` or `upsert`. To
save despite a duplicate-rule alert, pass `allow_save=True`. See
[Request options](#request-options) for every value.

```python
lead_id = await leads.create({"LastName": "Hamilton", "Company": "NASA"}, assignment_rule_id=True)
```

## Describe an object

`describe()` returns the fields and metadata of the object. If the cached copy is fresh, the
client returns it without a request. To skip the cache, pass `cached=False`.
`sf.describe_global()` lists every sObject in the org.

```python
describe = await leads.describe()
names = [f["name"] for f in describe["fields"]]
everything = await sf.describe_global()
print(len(everything["sobjects"]), names[:3])

fresh = await leads.describe(cached=False)
```

## Changed and deleted records

`updated(start, end)` and `deleted(start, end)` return the ids changed or deleted in a time
window. Datetimes must be timezone-aware. The client sends them in UTC.

```python
from datetime import datetime, timedelta, timezone

end = datetime.now(timezone.utc)
start = end - timedelta(days=1)
changed = await leads.updated(start, end)  # {"ids": [...], "latestDateCovered": "..."}
removed = await leads.deleted(start, end)  # {"deletedRecords": [...], ...}
print(len(changed["ids"]), len(removed["deletedRecords"]))
```

## Methods

| Method | REST call | Returns |
|---|---|---|
| `create(data, *, assignment_rule_id=None, allow_save=None)` | `POST sobjects/{o}` | the new id (`str`) |
| `get(record_id, fields=None, *, model=None, if_modified_since=None)` | `GET sobjects/{o}/{id}` | the record (`None` on `304`) |
| `update(record_id, data, *, assignment_rule_id=None, allow_save=None, if_unmodified_since=None)` | `PATCH sobjects/{o}/{id}` | `None` |
| `delete(record_id, *, if_unmodified_since=None)` | `DELETE sobjects/{o}/{id}` | `None` |
| `upsert(field, value, data, *, assignment_rule_id=None, allow_save=None)` | `PATCH sobjects/{o}/{field}/{value}` | `UpsertResult(id, created)` |
| `get_by_external_id(field, value, fields=None, *, model=None)` | `GET sobjects/{o}/{field}/{value}` | the record |
| `describe(*, cached=True)` | `GET sobjects/{o}/describe` | describe `dict` (cached) |
| `metadata()` | `GET sobjects/{o}` | basic info and recent items |
| `updated(start, end)` / `deleted(start, end)` | `GET sobjects/{o}/updated/` / `deleted/` | ids changed or deleted in the window |
| `sf.describe_global(*, cached=True)` | `GET sobjects` | list of sObjects (cached) |

## Request options

| Keyword | Header | Effect |
|---|---|---|
| `assignment_rule_id="01Q..."` | `Sforce-Auto-Assign: 01Q...` | run that assignment rule |
| `assignment_rule_id=True` / `False` | `Sforce-Auto-Assign: TRUE` / `FALSE` | run the default rule, or none |
| `allow_save=True` / `False` | `Sforce-Duplicate-Rule-Header: allowSave=...` | save despite a duplicate-rule alert (or not) |
| `if_modified_since=dt` (get) | `If-Modified-Since` | returns `None` when the record hasn't changed (`304`) |
| `if_unmodified_since=dt` (update, delete) | `If-Unmodified-Since` | `esf.Conflict` (`412`) if the record changed after `dt` |

## Errors

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

## How it works

### Value encoding

The client encodes values as JSON. The rules are the same with orjson and the stdlib:

- The client sends a `Decimal` as an exact JSON number, never through `float`.
- NaN and ±Infinity raise `ValueError` before the client sends anything. So a field never
  becomes blank by accident.
- `datetime` and `date` become ISO 8601 strings.
- `set`, `frozenset` and `tuple` become arrays.

```python
from decimal import Decimal

opp_id = await sf.sobject("Opportunity").create(
    {"Name": "Renewal", "StageName": "Prospecting", "CloseDate": "2027-01-31", "Amount": Decimal("12500.10")}
)
await sf.aclose()
```

### Validation and URLs

The client checks names and ids before it builds a URL:

- sObject names must match `^[A-Za-z][A-Za-z0-9_]*$`.
- Record ids must have 15 or 18 alphanumeric characters.
- Field names must pass `esf.soql_identifier()`. See [Field and object names](query.md#field-and-object-names).

If a value fails the check, the client raises `ValueError` and sends nothing.

The client percent-encodes an external id value as one path segment. For example, a space
becomes `%20`, `/` becomes `%2F` and `+` becomes `%2B`. If the value matches several records,
Salesforce returns `300 Multiple Choices`. The client raises this as `esf.MultipleMatches`.

### Describe cache

The client keys the describe cache by org, API version, sObject and a hash of the user id. The
key includes the user because field-level security differs by user.

The client uses a cached entry for `ttl` seconds without asking Salesforce. The default is 1
hour. After that, the client revalidates the entry with `If-Modified-Since`. A
`304 Not Modified` keeps the cached copy and costs no download. The client keeps stale entries
for revalidation for `stale_ttl`, which is 24 hours by default.

To configure the cache, pass `describe_cache=esf.DescribeCacheConfig(...)`. To share describe
results across processes, pass your own `cache=`. It must have `get`, `set(key, value, ttl)` and
`delete`, and hold plain JSON data.

### Clear the cache after a deploy

If a deploy adds or changes fields, the cached describe results are out of date until `ttl`
ends. To start fresh at once, pass your own `esf.MemoryCache` and call `clear()` after the deploy.

```python
cache = esf.MemoryCache()
async with esf.AsyncSalesforce(auth, cache=cache) as sf:
    await sf.sobject("Lead").describe()  # cached
    cache.clear()  # after the deploy: the next describe() asks Salesforce again
    await sf.sobject("Lead").describe()
```

To refresh one object only, call `describe(cached=False)`.
