# Query and search

`sf.query()` and its siblings run SOQL queries, and `sf.search()` runs SOSL searches. Pass
values after the query, and the client quotes them for you.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

page = await sf.query("SELECT Id, Name, Status FROM Lead WHERE Status = {}", "Open")
for lead in page:
    print(lead["Name"])
```

## Pass values to a query {#values-in-the-query}

Every method that takes SOQL also takes values for its `{}` placeholders. Pass them after the
query. The client quotes and escapes each value as a SOQL literal, with the same code as
[`esf.soql()`](#build-a-query-in-pieces).

!!! warning "An f-string looks the same and escapes nothing"
    Don't put values into SOQL with an f-string. `f"... WHERE Email = '{email}'"` breaks on
    `o'brien@example.com`. It also lets whoever controls `email` change your query.

```python
email = "o'brien@example.com"
page = await sf.query("SELECT Id, Name FROM Lead WHERE Email = {} AND Status IN {}", email, ["Open"])
print(page.total_size)
```

Only positional `{}` placeholders work here. The number of `{}` must equal the number of
values. If it doesn't, the client raises `ValueError`. So a forgotten placeholder can't drop a
filter. If you pass no values, the client sends the query exactly as written, braces included.

## Get one page

`query()` returns one page as a `QueryResult`. Iterating it iterates its records.

```python
page = await sf.query("SELECT Id, Name, Status FROM Lead WHERE Status = {}", "Open")
print(page.total_size, page.done, len(page))
```

`page_size=` asks for 200 to 2,000 rows per page. Salesforce treats it as a hint.
`include_deleted=True` also returns deleted and archived rows. See [`QueryResult`](#queryresult)
for every attribute and keyword.

## Get every row

`query_iter()` yields rows one by one and fetches the next page only when it needs it. On
`AsyncSalesforce` it is an async iterator.

```python
async for lead in sf.query_iter("SELECT Id, Email FROM Lead", page_size=500):
    pass  # process the row

emails = [r["Email"] async for r in sf.query_iter("SELECT Email FROM Lead WHERE Status = 'Open'")]
print(len(emails))
```

On the sync client, use `for lead in sf.query_iter(...)` or `list(sf.query_iter(...))`.
`query_iter` takes values, `page_size`, `include_deleted` and `model` like `query`.

### Complete child subqueries

Salesforce pages the rows of a parent-child subquery separately. A parent row may arrive with
only the first batch of its children. If you pass `expand_children=True`, `query_iter` gets the
rest before it yields the parent:

```python
soql = "SELECT Id, Name, (SELECT Id, LastName FROM Contacts) FROM Account"
async for account in sf.query_iter(soql, expand_children=True):
    contacts = account["Contacts"]
    if contacts:
        assert contacts["done"] and "nextRecordsUrl" not in contacts
        print(account["Name"], len(contacts["records"]))
```

Each extra child page costs one API call (`operation="query_more"`). Without the flag, children
arrive exactly as Salesforce sent them.

## Get exactly one row

Use `query_one()` when the query must match exactly one record, such as a lookup by a unique
field. No rows raise `esf.NotFound`; more than one raise `esf.MultipleMatches`.

```python
lead = await sf.query_one("SELECT Id, Name FROM Lead WHERE Email = {}", "ada@example.com")
print(lead["Name"])

try:
    await sf.query_one("SELECT Id FROM Lead WHERE Status = {}", "Open")
except esf.MultipleMatches as exc:
    print(len(exc.matches), "ids on the first page")
except esf.NotFound:
    print("no lead")
```

`query_one()` reads only the first page and decides from `totalSize`. `.matches` holds the
record ids on that page. The client raises both errors itself, so `status` is `None` and
`operation` is `"query"`. The client sends the SOQL as written and adds no `LIMIT`.
`query_one()` takes `include_deleted=` and `model=`.

## Get the first row

Use `query_first()` when you want one row if there is one, such as the newest record. It
returns `None` instead of raising.

```python
newest = await sf.query_first("SELECT Id, Name FROM Lead ORDER BY CreatedDate DESC LIMIT 1")
print(newest is None or "Id" in newest)
```

It reads only the first page. Add `LIMIT 1` yourself, so Salesforce doesn't return more rows
than you need.

## Count rows

`count()` runs a `SELECT COUNT() ...` query and returns `totalSize` as an `int`.

```python
n = await sf.count("SELECT COUNT() FROM Lead WHERE Status = {}", "Open")
print(n)
```

For `COUNT(Id)` and other aggregates, use `query()` and read the aggregate row.

## Select all fields

SOQL has no `SELECT *`. Use `FIELDS(STANDARD)`, `FIELDS(CUSTOM)` or `FIELDS(ALL)` in the field
list instead.

```python
page = await sf.query("SELECT FIELDS(STANDARD) FROM Lead WHERE Status = {}", "Open")
everything = await sf.query("SELECT FIELDS(ALL) FROM Lead ORDER BY CreatedDate LIMIT 200")
print(len(page), len(everything))
```

`FIELDS(STANDARD)` works in every query. `FIELDS(ALL)` and `FIELDS(CUSTOM)` need `LIMIT` 200 or
less, or `WHERE Id IN` with at most 200 ids. See
[FIELDS()](https://developer.salesforce.com/docs/atlas.en-us.soql_sosl.meta/soql_sosl/sforce_api_calls_soql_select_fields.htm).
Bulk query jobs don't accept `FIELDS(ALL)` or `FIELDS(CUSTOM)`. See
[Bulk API 2.0](bulk.md#all-fields).

To read every field of more than 200 rows, build the field list from `describe()`:

```python
describe = await sf.sobject("Lead").describe()
names = ", ".join(f["name"] for f in describe["fields"])
async for lead in sf.query_iter(f"SELECT {names} FROM Lead"):
    pass  # every field the user can read
```

`describe()` returns only the fields that the user can see. The client caches it, so this costs
no request most of the time.

## Continue later with a cursor {#cursors}

`page.cursor` points at the next page. Keep it and pass it to `query_more()` later.

```python
first = await sf.query("SELECT Id, Name FROM Lead", page_size=200)
second = await sf.query_more(first.cursor)  # or the string the browser sent back
print(len(first), len(second), second.done)
```

`query_more()` takes `page_size=` and `model=`, like `query()`.

Salesforce expires a query locator after about 15 minutes of inactivity. If the locator
expired, `query_more()` raises `MalformedQuery` with `error_code="INVALID_QUERY_LOCATOR"`.

### Signed cursors

An unsigned cursor is the query locator path in base64. Anyone can decode it or write a new
one. Send only signed cursors to a browser or another client, for example as a `?cursor=` query
parameter.

Pass `cursor_key=` to the client to sign cursors with HMAC-SHA256. Then `query_more()` raises
`esf.MalformedQuery` with `error_code="INVALID_CURSOR"` for these cursors:

- a signed cursor that someone changed
- a cursor signed with a different key
- an unsigned cursor

```python
signed = esf.AsyncSalesforce(auth, cursor_key=b"a long random secret from your config")
page = await signed.query("SELECT Id FROM Lead", page_size=200)
try:
    await signed.query_more(page.cursor[:-2] + "xx")
except esf.MalformedQuery as exc:
    print(exc.error_code)  # INVALID_CURSOR
await signed.aclose()
```

A signed cursor is not bound to a user. Anyone who has it can read the next page with your
client's permissions. Check in your app that the user may see the query results before you call
`query_more()`.

## Build a query in pieces

`esf.soql()` works like `str.format`. It replaces each `{}` or `{name}` placeholder with the
value, quoted as a SOQL literal. Use it to build a query from parts, or if you need `{name}` or
`{:like}`. In other cases, pass values to the method directly.

```python
status, companies = "Open", ["Acme", "O'Reilly"]
q = esf.soql(
    "SELECT Id, Name FROM Lead WHERE Status = {} AND Company IN {companies} AND IsConverted = {}",
    status,
    False,
    companies=companies,
)
print(q)
# SELECT Id, Name FROM Lead WHERE Status = 'Open' AND Company IN ('Acme','O\'Reilly') AND IsConverted = false
```

Use `{{` and `}}` for literal braces. See [Quoting rules](#quoting-rules) for how each Python
type is written.

### `LIKE` patterns

Inside a `LIKE`, `%` and `_` are wildcards. `{:like}` (or `esf.soql_like()`) escapes them, so the
value matches exactly. Add your own wildcards with the pattern argument of `soql_like`:

```python
term = "50%_off"
exact = esf.soql("SELECT Id FROM Campaign WHERE Name LIKE {:like}", term)
contains = esf.soql("SELECT Id FROM Campaign WHERE Name LIKE {}", esf.soql_like(term, "%{}%"))
print(contains)  # ... LIKE '%50\%\_off%'
```

### Field and object names

Placeholders quote values, not names. Validate a dynamic field or object name with
`esf.soql_identifier()`, and the name is inserted unquoted:

```python
sort_field = esf.soql_identifier("CreatedDate")
q = esf.soql("SELECT Id FROM Lead ORDER BY {} DESC LIMIT {}", sort_field, 10)
print(q)
```

It accepts `Name`, `Custom__c`, `ns__Field__c` and dotted relationship paths like `Owner.Name`.
Anything else raises `ValueError`.

## Export rows {#exporting-rows-esfflatten}

Query rows are nested: `attributes` at every level, lookups as nested dicts, and child
subqueries as `{"totalSize", "done", "records"}`. `esf.flatten()` turns one row into a flat
dict for CSV, DataFrames or JSON.

```python
row = {
    "attributes": {"type": "Account"},
    "Name": "Acme",
    "Owner": {"attributes": {"type": "User"}, "Name": "Ada"},
    "Parent": None,
    "Contacts": {"totalSize": 1, "done": True, "records": [{"attributes": {}, "LastName": "Byron"}]},
}
print(esf.flatten(row))
# {'Name': 'Acme', 'Owner.Name': 'Ada', 'Parent': None, 'Contacts': [{'LastName': 'Byron'}]}
```

- Lookups become dotted keys, such as `"Owner.Name"` and `"Owner.Manager.Email"`.
- A null lookup stays one key, such as `"Parent": None`. So read it with
  `row.get("Parent.Name")`.
- A child subquery becomes a list of the flattened child rows that are present. Use
  `query_iter(..., expand_children=True)` to get all of them.
- `model=` instances aren't dicts, so `flatten` raises `TypeError` for them.

## Explain a query

`explain()` asks Salesforce how it would run a query, without running it. The first plan is the
most optimal. Salesforce labels this feature Beta.

```python
plans = await sf.explain("SELECT Id FROM Lead WHERE Email = {}", "ada@example.com")
best = plans[0]
print(best.leading_operation, best.relative_cost, best.sobject_type)
for note in plans[-1].notes:
    print(note.description, note.fields)
```

Each `esf.QueryPlan` has these attributes:

| Attribute | Meaning |
|---|---|
| `leading_operation` | `"Index"`, `"Other"` or `"TableScan"` |
| `relative_cost` | above 1.0 is not selective |
| `cardinality`, `sobject_cardinality` | estimated rows, and rows in the object |
| `sobject_type` | the sObject |
| `fields` | indexed fields used |
| `notes` | `esf.QueryPlanNote` items: why a field couldn't use an index |

## Search

`search()` runs a SOSL query. `parameterized_search()` takes the search term as a plain string,
so you don't build SOSL yourself.

```python
found = await sf.search("FIND {Lovelace} IN NAME FIELDS RETURNING Lead(Id, Name)")
print([r["Name"] for r in found])

found = await sf.parameterized_search(
    "Lovelace",
    sobjects=["Lead", {"name": "Contact", "fields": ["Id", "Name"], "limit": 5}],
    fields=["Id", "Name"],
    limit=20,
)
print(len(found))
await sf.aclose()
```

SOSL braces (`FIND {...}`) are SOSL syntax, not `esf.soql()` placeholders, and SOQL quoting
does not protect them. Wrap user input in `esf.sosl_term(value)`. It returns the term with its
braces and escapes every SOSL reserved character, so the input can't close the braces or add
wildcards. Words like `AND` and `OR` still act as SOSL operators. You can also use
`parameterized_search`.

`parameterized_search` builds the request body from its arguments:

- Each `sobjects` item is a name or a full spec dict.
- The client sends `limit` as `overallLimit`.
- Any other keyword goes into the body unchanged. For a key that is a Python keyword, use
  `**{"in": "NAME"}`.

## Methods

| You want | Use | Returns |
|---|---|---|
| one page, plus a cursor to continue later | `query()` then `query_more(cursor)` | `QueryResult` |
| every row, fetched page by page as you go | `query_iter()` | an (async) iterator of rows |
| exactly one row, and an error otherwise | `query_one()` | the row; `NotFound` / `MultipleMatches` |
| the first row if there is one | `query_first()` | the row or `None` |
| how many rows match | `count()` with `SELECT COUNT() ...` | `int` |
| how Salesforce would run a query | `explain()` (`GET query?explain=`) | `list[QueryPlan]` |
| rows as flat dicts for CSV or DataFrames | `esf.flatten(row)` | `dict` |
| a SOSL search | `search(sosl)` (`GET search?q=`) | list of `searchRecords` |
| a search from a plain term | `parameterized_search(q, *, sobjects=None, fields=None, limit=None, **extra)` (`POST parameterizedSearch`) | list of records |

## `QueryResult`

| Attribute | Meaning |
|---|---|
| `records` | rows of this page (`dict`s, or `model` instances) |
| `total_size` | `totalSize` for the whole query |
| `done` | `True` on the last page |
| `cursor` | an opaque `esf.Cursor` for the next page, or `None` when `done` |

| Keyword | Effect |
|---|---|
| `page_size=` | sends `Sforce-Query-Options: batchSize=N`; must be 200–2000 (`ValueError` otherwise); a hint to Salesforce |
| `include_deleted=True` | uses `queryAll` (deleted and archived rows too) |
| `model=` | converts rows (see [Records → Typed records](records.md#typed-records-model)) |

## How it works

### Quoting rules

`esf.soql()` and the values you pass to query methods use these rules:

| Python value | SOQL literal |
|---|---|
| `str` | `'...'` with `\ ' " \n \r \t \b \f` escaped |
| `int`, `float`, `Decimal` | the number in plain notation (never `1e16`); NaN/Infinity raise `ValueError` |
| `bool` | `true` / `false` |
| `None` | `null` |
| `datetime` (timezone-aware) | `2026-11-09T12:00:00Z` (converted to UTC); a naive datetime raises `ValueError` |
| `date` | `2026-11-09` |
| `list`, `tuple`, `set`, `frozenset` | `('a','b')`; an empty or nested sequence raises an error |
| anything else | `TypeError` |

`esf.soql()` rejects conversions such as `{!r}`, and format specs other than `like`.

### Cursor safety

A cursor is the `nextRecordsUrl` in base64url. When the client decodes a cursor, it checks it
against the exact shape of a Salesforce query locator path. So a cursor from a browser can't
make your server call any other URL.

### Counts and logging

`count()` raises `esf.UnexpectedResponse` if the response has no integer `totalSize`.

Query events carry `soql_hash`. It is the first 12 characters of a SHA-256 hash of the query,
with every literal replaced by `?`. The same query shape always gets the same hash, and the
client logs no values. With `log_soql="redacted"`, the event also carries that normalised text. See
[Logging](../logging.md).
