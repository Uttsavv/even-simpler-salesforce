# Query and search

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)
```

## Building SOQL safely: `esf.soql()`

Never paste values into SOQL with f-strings. `esf.soql()` works like `str.format`: each `{}`
or `{name}` placeholder is replaced with the value quoted as a SOQL literal.

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

Use `{{` and `}}` for literal braces. Conversions (`{!r}`) and format specs other than `like`
are rejected.

### `LIKE` patterns

Inside a `LIKE`, `%` and `_` are wildcards. `{:like}` (or `esf.soql_like()`) escapes them so the
value matches exactly. Add your own wildcards with `soql_like`'s pattern:

```python
term = "50%_off"
exact = esf.soql("SELECT Id FROM Campaign WHERE Name LIKE {:like}", term)
contains = esf.soql("SELECT Id FROM Campaign WHERE Name LIKE {}", esf.soql_like(term, "%{}%"))
print(contains)  # ... LIKE '%50\%\_off%'
```

### Field and object names

Field and object names are not values, so placeholders don't quote them. Validate dynamic names
with `esf.soql_identifier()`. It accepts `Name`, `Custom__c`, `ns__Field__c` and dotted
relationship paths like `Owner.Name`, and raises `ValueError` for anything else. The validated
name is inserted unquoted:

```python
sort_field = esf.soql_identifier("CreatedDate")
q = esf.soql("SELECT Id FROM Lead ORDER BY {} DESC LIMIT {}", sort_field, 10)
print(q)
```

## `query()`: one page

```python
page = await sf.query(esf.soql("SELECT Id, Name, Status FROM Lead WHERE Status = {}", "Open"))
print(page.total_size, page.done, len(page))
for lead in page:  # iterating a page iterates its records
    print(lead["Name"])
```

`query()` returns a `QueryResult` with:

| Attribute | Meaning |
|---|---|
| `records` | rows of this page (`dict`s, or `model` instances) |
| `total_size` | `totalSize` for the whole query |
| `done` | `True` on the last page |
| `cursor` | an opaque `esf.Cursor` for the next page, or `None` when `done` |

| Keyword | Effect |
|---|---|
| `page_size=` | sends `Sforce-Query-Options: batchSize=N`; must be 200–2000 (`ValueError` otherwise). Salesforce treats it as a hint |
| `include_deleted=True` | uses `queryAll` (deleted and archived rows too) |
| `model=` | converts rows (see [Records → model](records.md#typed-records-model)) |

## Cursors

`page.cursor` is safe to send to a browser and get back later, for example as a
`?cursor=` query parameter. It is the base64url-encoded `nextRecordsUrl`. When decoded, it is
checked against the exact shape of a Salesforce query locator path, so a cursor sent by a client
can't make your server call any other URL.

```python
first = await sf.query("SELECT Id, Name FROM Lead", page_size=200)
second = await sf.query_more(first.cursor)  # or the string the browser sent back
print(len(first), len(second), second.done)
```

Pass `cursor_key=` to **sign** cursors with HMAC-SHA256. A signed cursor that was changed, or
was signed with a different key, raises `esf.MalformedQuery` (`error_code="INVALID_CURSOR"`),
and so does an unsigned cursor given to a client that expects signed ones:

```python
signed = esf.AsyncSalesforce(auth, cursor_key=b"a long random secret from your config")
page = await signed.query("SELECT Id FROM Lead", page_size=200)
try:
    await signed.query_more(page.cursor[:-2] + "xx")
except esf.MalformedQuery as exc:
    print(exc.error_code)  # INVALID_CURSOR
await signed.aclose()
```

Query locators expire on the Salesforce side (after about 15 minutes of inactivity). An
expired locator raises `MalformedQuery` with `error_code="INVALID_QUERY_LOCATOR"`.

## `query_iter()`: every row

`query_iter()` yields rows one by one and fetches the next page only when it is needed. On
`AsyncSalesforce` it is an async iterator:

```python
async for lead in sf.query_iter("SELECT Id, Email FROM Lead", page_size=500):
    pass  # process the row

emails = [r["Email"] async for r in sf.query_iter("SELECT Email FROM Lead WHERE Status = 'Open'")]
print(len(emails))
```

On the sync client: `for lead in sf.query_iter(...)`, or `list(sf.query_iter(...))`.
`query_iter` takes `page_size`, `include_deleted` and `model` like `query`.

### Child subqueries: `expand_children=True`

Salesforce paginates the rows of a parent-child subquery separately. A parent row may arrive
with only the first batch of its children, plus a `nextRecordsUrl` for the rest. With
`expand_children=True`, `query_iter` follows those links before it yields the parent, so each
child list is complete:

```python
soql = "SELECT Id, Name, (SELECT Id, LastName FROM Contacts) FROM Account"
async for account in sf.query_iter(soql, expand_children=True):
    contacts = account["Contacts"]
    if contacts:
        assert contacts["done"] and "nextRecordsUrl" not in contacts
        print(account["Name"], len(contacts["records"]))
```

Without it, the children arrive exactly as Salesforce sent them. Each extra child page costs
one API call (`operation="query_more"`).

## `count()`

```python
n = await sf.count(esf.soql("SELECT COUNT() FROM Lead WHERE Status = {}", "Open"))
print(n)
```

`count()` runs a `SELECT COUNT() ...` query and returns `totalSize` as an `int`. A response
without an integer `totalSize` raises `esf.UnexpectedResponse`. For `COUNT(Id)` and other
aggregates, use `query()` and read the aggregate row.

## Search

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

- `search(sosl)` sends `GET search?q=` and returns `searchRecords` as a list.
- `parameterized_search(q, *, sobjects=None, fields=None, limit=None, **extra)` sends
  `POST parameterizedSearch`. `sobjects` items are names or full spec dicts, `limit` is sent
  as `overallLimit`, and any other keyword goes into the body unchanged. For a key that is a
  Python keyword, use `**{"in": "NAME"}`.

SOSL braces (`FIND {...}`) are literal SOSL syntax, not `esf.soql()` placeholders. Escape user
input for SOSL yourself, or use `parameterized_search`, which takes the search term as a plain
string.

## What gets logged

Query events carry `soql_hash`, a 12-character SHA-256 prefix of the query with every literal
replaced by `?`. So the same query shape always has the same hash, and no values are logged.
With `log_soql="redacted"`, the event also carries that normalised text. See
[Logging](../logging.md).
