# Composite requests

`sf.composite` sends many REST calls in one round trip. Use it when later calls need the
results of earlier ones, or when a group of writes must succeed or fail together.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

c = sf.composite.builder(all_or_none=True)
acc = c.create("Account", {"Name": "Acme"})
c.create("Contact", {"LastName": "Doe", "AccountId": acc.ref("id")})
result = await c.send()
result.raise_for_errors()
print(result[0].body["id"])  # '001...'
```

`sf.composite` has four request types:

| Method | REST call | Use it to |
|---|---|---|
| `builder()` | `POST composite` | run up to 25 calls in order, with references |
| `graph()` | `POST composite/graph` | run several groups of up to 500 calls, each group atomic |
| `tree(sobject, records)` | `POST composite/tree/{sobject}` | create parent and child records together |
| `batch()` | `POST composite/batch` | run up to 25 independent calls |

## Build a Composite request

Get a builder from `sf.composite.builder()`. Each method adds one subrequest and returns a
`Reference`. Call `send()` when you have added every subrequest.

| Builder method | REST call |
|---|---|
| `create(sobject, data)` | `POST sobjects/{sobject}` |
| `update(sobject, record_id, data)` | `PATCH sobjects/{sobject}/{id}` |
| `upsert(sobject, field, value, data)` | `PATCH sobjects/{sobject}/{field}/{value}` |
| `delete(sobject, record_id)` | `DELETE sobjects/{sobject}/{id}` |
| `get(sobject, record_id, fields=None)` | `GET sobjects/{sobject}/{id}` |
| `query(soql, *values)` | `GET query?q=` |
| `request(method, path, body=None)` | any other resource, for example sObject Collections |

Every method also takes `ref_id=`. If you leave it out, the builder makes one, such as
`Contact2`.

## Use an earlier result

Call `ref(field)` on a `Reference` to use a field of that result in a later subrequest.
Salesforce replaces `@{refId.field}` with the value before it runs the subrequest.

```python
c = sf.composite.builder()
acc = c.create("Account", {"Name": "Globex"}, ref_id="acc")
c.update("Account", acc.ref("id"), {"Name": "Globex Corp"})
c.get("Account", acc.ref("id"), ["Name"], ref_id="read")
c.query("SELECT Id FROM Account WHERE Name = {}", "Globex Corp", ref_id="found")
result = await c.send()
print(result["read"].body["Name"])  # 'Globex Corp'
print(result["found"].body["totalSize"])  # 1
```

A query result is a page of records, so use a path such as `ref("records[0].Id")`. The
builder raises `ValueError` if a subrequest refers to a reference id that wasn't added before
it.

## Check the results

`send()` returns a `CompositeResult` with one `SubrequestResult` per subrequest, in the order
you added them. Index it by position or by reference id. A failed subrequest doesn't raise.

```python
c = sf.composite.builder(all_or_none=True)
c.create("Lead", {"LastName": "Curie", "Company": "Sorbonne"}, ref_id="ok")
c.create("Lead", {"LastName": "Franklin", "Company": "King's", "Country": "UK"}, ref_id="bad")
result = await c.send()
print([r.status for r in result])  # [400, 400]
print(result["ok"].halted)  # True: rolled back because "bad" failed
try:
    result.raise_for_errors()
except esf.ValidationRuleFailed as exc:
    print(exc.message)  # '[ADDR_REQUIRED] Street is required when Country is set'
```

`raise_for_errors()` raises the typed error of the subrequest that caused the failure. It
uses the same classes as single calls, such as `ValidationRuleFailed` or `NotFound`. A
subrequest that Salesforce only rolled back or skipped has `halted` set. It doesn't pick the
class. The exception's `.errors` holds the error entries of every failed subrequest. Each
entry has `extended["referenceId"]`.

| `SubrequestResult` field | Meaning |
|---|---|
| `reference_id` | the subrequest's reference id |
| `status` | HTTP status of the subrequest |
| `body` | decoded body: the new id for a create, the record for a get, `None` for 204 |
| `headers` | response headers, such as `Location` |
| `error` | the typed error of a failed subrequest, else `None` |
| `ok`, `halted`, `errors` | success flag, skipped or rolled back, error entries |

## Run several graphs

A Composite Graph request holds several graphs. Each graph runs in its own transaction. If a
subrequest fails, Salesforce rolls back that graph only.

```python
g = sf.composite.graph()
first = g.graph("first")
a = first.create("Account", {"Name": "Initech"})
first.create("Contact", {"LastName": "Lumbergh", "AccountId": a.ref("id")})
second = g.graph("second")
second.create("Lead", {"LastName": "Waddams"})  # no Company: this graph fails
results = await g.send()
print([(r.graph_id, r.ok) for r in results])  # [('first', True), ('second', False)]
```

Each `GraphResult` has `graph_id`, `ok` and `results`, a `CompositeResult`. Call
`raise_for_errors()` on a graph to raise the error that made it fail. A graph can't contain
query subrequests.

## Create record trees

`tree()` creates root records with their child records in one transaction. Put child records
in a child relationship field as `{"records": [...]}`. Each child needs `attributes.type`.

```python
ids = await sf.composite.tree(
    "Account",
    [
        {
            "attributes": {"referenceId": "hooli"},
            "Name": "Hooli",
            "Contacts": {
                "records": [{"attributes": {"type": "Contact", "referenceId": "gavin"}, "LastName": "Belson"}]
            },
        }
    ],
)
print(ids["hooli"], ids["gavin"])  # '001...' '003...'
```

The call returns `{referenceId: id}` for every record. Records without a `referenceId` get
`ref1`, `ref2` and so on, in depth-first order. If Salesforce rejects one record, it saves
nothing. The call then raises the typed error of the first rejected record.

## Run independent subrequests

A Batch request runs up to 25 subrequests that don't depend on each other. Each one is its
own transaction. The builder has the same methods as `builder()`, but references are not
allowed.

```python
b = sf.composite.batch(halt_on_error=True)
b.get("Account", ids["hooli"], ["Name"], ref_id="hooli")
b.query("SELECT Id FROM Contact WHERE LastName = {}", "Belson", ref_id="contacts")
result = await b.send()
print(result["hooli"].body["Name"])  # 'Hooli'
```

With `halt_on_error=True`, Salesforce skips the remaining subrequests after the first failure.

## Limitations

The client checks every limit when you add a subrequest. If a limit is broken, it raises
`ValueError` and sends nothing.

| Request | Limit |
|---|---|
| Composite | 25 subrequests; at most 5 of them sObject Collections or query operations |
| Composite Graph | 500 subrequests per graph; no queries |
| sObject Tree | 200 records across all trees; 5 record types; 5 levels |
| Batch | 25 subrequests; no references |

```python
c = sf.composite.builder()
for i in range(5):
    c.query("SELECT Id FROM Account LIMIT 1")
try:
    c.query("SELECT Id FROM Contact LIMIT 1")
except ValueError as exc:
    print(exc)  # at most 5 of the 25 subrequests of a Composite request may be ...
```

## How it works

A request that writes is never resent after it may have reached Salesforce. If it times out
or gets a 5xx, the client raises the error and doesn't retry. The writes may or may not have
been saved. [ADR 0001](../adr/0001-retry-safety.md) explains why. A request made only of
`get` and `query` subrequests is a read, so the client retries it as usual.

The client compresses request bodies larger than 64 KiB with gzip. Salesforce documents the
resources in the REST API Developer Guide:
[Composite](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_composite_post.htm),
[Composite Graph](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_graph.htm),
[sObject Tree](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_sobject_tree.htm)
and
[Batch](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_batch.htm).

In tests, `esf.testing.FakeSalesforce` has the same `composite` area. It runs subrequests
against the fake's records and resolves references.
