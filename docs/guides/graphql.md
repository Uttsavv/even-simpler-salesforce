# GraphQL

`sf.graphql()` runs a query against the Salesforce GraphQL API and returns its `data`. Use it
to get only the fields you need, from one or more objects, in one call.

```python
import even_simpler_salesforce as esf

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
sf = esf.AsyncSalesforce(auth)

data = await sf.graphql("""
    query {
      uiapi {
        query {
          Lead(first: 2) {
            edges { node { Id Name { value } Company { value } } }
          }
        }
      }
    }
""")
for edge in data["uiapi"]["query"]["Lead"]["edges"]:
    print(edge["node"]["Name"]["value"])  # Ada Lovelace
```

The call returns the `data` member of the response as a dict. Field values sit in
`{"value": ...}` objects, as the GraphQL API sends them.

## Pass variables

Declare the variables in the query and pass their values as a dict. Don't format values into
the query text yourself.

```python
query = """
    query Leads($first: Int) {
      uiapi { query { Lead(first: $first) { edges { node { Id Email { value } } } } } }
    }
"""
data = await sf.graphql(query, {"first": 5})
print(len(data["uiapi"]["query"]["Lead"]["edges"]))  # 5
```

## Read every page

`sf.graphql_iter()` yields every node of a connection and fetches the next page when it needs
it. The query must declare an `$after` variable and pass it to the connection. It must also
select `pageInfo { hasNextPage endCursor }`.

```python
query = """
    query Leads($after: String) {
      uiapi {
        query {
          Lead(first: 200, after: $after) {
            edges { node { Id Name { value } } }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
"""
names = [lead["Name"]["value"] async for lead in sf.graphql_iter(query, path="uiapi.query.Lead")]
print(len(names))  # 450
```

`path` is the dotted path from `data` to the connection. The client sets `after` to the
`endCursor` of each page and yields each `edges[].node`. Pass other variables in the second
argument, as with `sf.graphql()`.

If the query has no `$after` variable, the call raises `ValueError` before it sends anything.

## Handle errors

If the response has an `errors` array, the call raises `esf.GraphQLError`. Salesforce answers
a query with an unknown field with `200 OK` and an `errors` array, so the status does not tell
you that the query failed.

```python
try:
    await sf.graphql("query { uiapi { query { Lead { edges { node { Nmae { value } } } } } } }")
except esf.GraphQLError as exc:
    print(exc.error_code)  # ValidationError
    print(exc.graphql_errors[0]["locations"])  # [{'line': 1, 'column': 47}]
```

`GraphQLError` has these attributes, on top of the ones every error has:

| Attribute | Type | Description |
|---|---|---|
| `graphql_errors` | `tuple[Mapping, ...]` | the `errors` entries as Salesforce sent them |
| `errors` | `tuple[ErrorDetail, ...]` | the same entries; `extended` holds `path`, `locations` and `extensions` |
| `error_code` | `str \| None` | `extensions.ErrorType` of the first entry, such as `ValidationError` |
| `data` | `Any` | the partial `data` that came with the errors, or `None` |

### Use partial data

A response can hold `data` for some fields and `errors` for others. The call still raises,
so you never use incomplete data by mistake. If part of the data is enough, read it from
`exc.data`. The `path` of each error tells you which field failed.

```python
query = "query { uiapi { query { Lead(first: 1) { edges { node { Id } } } } } }"
try:
    data = await sf.graphql(query)
except esf.GraphQLError as exc:
    failed = [e.get("path") for e in exc.graphql_errors]
    data = exc.data or {}
```

## Test code that uses GraphQL

`FakeSalesforce` answers GraphQL documents that you script with `fake.on_graphql()`. See
[Testing](../testing.md#script-graphql).

## How it works

- The client sends `POST /services/data/vXX.X/graphql` with the body
  `{"query": ..., "variables": ...}`.
- A query is a read. The client resends it after a timeout or a `5xx`, like a `GET`.
- A document with a `mutation` writes records. The client never resends it after it may have
  reached Salesforce.
- The client finds `query`, `mutation` and the declared variables with a light scan of the
  document. It does not validate the document. Salesforce does that.
- Each page of `sf.graphql_iter()` is one API call. A larger `first:` means fewer calls.
- If a page has a next page but no `endCursor`, or repeats a cursor, the call raises
  `esf.UnexpectedResponse`. This stops an endless loop.
- A REST error body such as `[{"errorCode": "API_DISABLED_FOR_ORG", ...}]` maps to the usual
  classes, for example `esf.Forbidden`.

## Limitations

- `sf.graphql_iter()` pages one connection. To page nested connections, call `sf.graphql()`
  with your own cursor variables.
- A document with more than one operation needs an operation name, and the client does not
  send one. Send one operation per call.
