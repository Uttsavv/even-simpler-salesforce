# Testing

`even_simpler_salesforce.testing` lets you test code that uses the client without a real org. Use
the fakes for unit tests, and record and replay for contract tests.

```python
import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import FakeSalesforce


def close_lead(sf, lead_id: str) -> None:  # the code under test
    sf.sobject("Lead").update(lead_id, {"Status": "Closed - Not Converted"})


def test_close_lead(esf_fake: FakeSalesforce) -> None:
    lead_id = esf_fake.add("Lead", {"LastName": "Doe", "Company": "Acme", "Status": "Open"})

    close_lead(esf_fake, lead_id)

    assert esf_fake.record("Lead", lead_id)["Status"] == "Closed - Not Converted"
    [call] = esf_fake.calls_for("sobject.update", sobject="Lead")
    assert call.args["data"] == {"Status": "Closed - Not Converted"}
```

The `esf_fake` fixture comes from the [pytest plugin](#pytest-plugin), which the package installs.

## Choose a tool

| Tool | Use it for | Network |
|---|---|---|
| `FakeSalesforce`, `AsyncFakeSalesforce` | unit tests of your own logic: an in-memory org with scripted queries and injected errors | none |
| `record()`, `replay()` | contract tests: capture real traffic once (redacted), then replay it through the real client | only while recording |
| pytest plugin | fixtures for the above, plus a marker for opt-in real-org tests | none |

`import even_simpler_salesforce.testing` needs neither pytest nor respx. The pytest plugin and the
token store contract suite need pytest. The `[testing]` extra installs pytest and respx.

## Use the fakes

`FakeSalesforce` is the sync fake and `AsyncFakeSalesforce` is the async fake. They share one
implementation. They have the methods of the real clients for these areas:

- records
- query and search
- Collections
- lead convert, merge and platform events
- [Files](guides/files.md)
- [GraphQL](guides/graphql.md), with scripted answers
- `request`, `limits` and `usage`
- `with_auth` and `tagged`

The fakes are duck-typed, not subclasses. If your code is type-checked against the real class, use
`typing.cast`.

## Seed and inspect records

The fake stores records per sObject. `create` returns valid-looking 18-character ids with the right
key prefix, such as `00Q` for Lead and `001` for Account.

The other record methods behave like the API:

- `get`, `update` and `delete` raise `NotFound` for missing ids.
- A second delete raises `NotFound(ENTITY_IS_DELETED)`.
- An ambiguous upsert raises `MultipleMatches`.
- `if_unmodified_since` raises `Conflict`.

The Collections methods follow the same rules.

| Control | Does |
|---|---|
| `fake.add(sobject, data)`, `add_many(sobject, rows)` | seed records without recording a call; returns the id or ids |
| `fake.records(sobject)`, `fake.record(sobject, id)` | copies of what is stored |
| `fake.new_id(sobject)` | a fresh id with the sObject's prefix |
| `fake.set_describe(sobject, describe)` | what `describe()` returns; otherwise the fake makes up a minimal one |
| `fake.set_limits({...})` | what `limits()` returns |
| `fake.set_api_usage(used, max)` | pretend the fake saw a `Sforce-Limit-Info` header; drives budgets |
| `fake.add_file(data, filename=..., link_to=...)` | seed a file without recording a call; returns the ContentVersion id |
| `fake.file_content(version_id)` | the bytes stored for a file, or `None` |

The fake doesn't run validation rules, triggers or required-field checks. To simulate them, use
[`reject()` or `fail_next()`](#inject-errors).

### Lead convert, merge and platform events

`fake.leads.convert()` creates the account, contact and opportunity in the fake's store. It
also marks the lead converted. `fake.merge()` deletes the duplicates and points fields that held
their ids at the kept record. `fake.published(name)` returns the events you published.

```python
from even_simpler_salesforce.testing import FakeSalesforce

fake = FakeSalesforce()
lead = fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
result = fake.leads.convert(lead, converted_status="Closed - Converted")
assert fake.record("Account", result.account_id)["Name"] == "Acme"

fake.platform_events.publish("Order_Event__e", {"Order_Number__c": "A-1"})
assert fake.published("Order_Event__e") == [{"Order_Number__c": "A-1"}]
```

`reject("Lead", error)` makes conversions fail, and `reject("Order_Event__e", error)` makes
publishes fail.

## Script queries

The fake has no SOQL engine, because a fake engine would give answers your org wouldn't. You script
the answers instead.

```python
import re


def test_open_leads(esf_fake: FakeSalesforce) -> None:
    esf_fake.add_many("Lead", [{"LastName": f"L{i}", "Company": "Acme"} for i in range(450)])
    script = esf_fake.on_query(
        re.compile(r"FROM Lead WHERE Status = 'Open'"),
        rows=lambda soql: esf_fake.records("Lead"),  # evaluated on each call
    )

    rows = list(esf_fake.query_iter("SELECT Id FROM Lead WHERE Status = 'Open'", page_size=200))

    assert len(rows) == 450
    assert script.hits == 1
    assert [c.operation for c in esf_fake.calls] == ["query", "query_more", "query_more"]
```

If a query, search or request has no matching script, the fake raises `UnscriptedCall`. It is an
`AssertionError`, so `except esf.SalesforceError` in your code can't swallow it.

| Method | Scripts | Matches on |
|---|---|---|
| `on_query(match, rows=(), *, total_size=None, times=None)` | `query()` and the methods built on it, such as `query_iter()` and `count()` | the SOQL text |
| `on_search(match, rows=(), *, times=None)` | `search()` and `parameterized_search()` | `q` |
| `on_request(method, path, response=None, *, times=None)` | `sf.request(...)` | `method` and `path` |

The arguments work like this:

- `match` is the SOQL text, a compiled regex or a predicate `(soql) -> bool`. The fake collapses
  whitespace before it compares text. A regex uses `re.search`.
- `rows` is a list or a callable.
- `total_size` defaults to the number of rows. `count()` returns it.
- `times` limits how often the script can match.
- If more than one script matches, the newest one wins.
- `path` is a string or a regex, which must match in full (`fullmatch`). The fake ignores a
  versioned `/services/data/vXX.X/` prefix.
- `response` is a value, a callable that gets a `FakeRequest`, or an exception to raise. The fake
  deep-copies a value.

Pages follow `page_size`, which defaults to 2,000. Cursors work, and so does signing with
`FakeSalesforce(cursor_key=...)`. `query_iter(..., expand_children=True)` is accepted but does
nothing, because scripted child records are already complete.

## Script GraphQL

The fake doesn't run GraphQL either. `on_graphql(match, data)` sets the `data` that
`graphql()` returns for a document. With `nodes=` and `path=`, the fake builds the connection
and pages it by the `after` variable, so `graphql_iter()` works unchanged.

```python
LEADS = """
    query Leads($after: String) {
      uiapi { query { Lead(first: 200, after: $after) {
        edges { node { Id } } pageInfo { hasNextPage endCursor }
      } } }
    }
"""


def test_graphql_paging(esf_fake: FakeSalesforce) -> None:
    nodes = [{"Id": esf_fake.new_id("Lead")} for _ in range(5)]
    esf_fake.on_graphql(re.compile(r"\bLead\b"), nodes=nodes, path="uiapi.query.Lead", page_size=2)

    assert list(esf_fake.graphql_iter(LEADS, path="uiapi.query.Lead")) == nodes
    assert len(esf_fake.calls_for("graphql")) == 3


def test_graphql_error(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_graphql("{ uiapi { a } }", esf.GraphQLError("bad field", error_code="ValidationError"))

    try:
        esf_fake.graphql("{ uiapi { a } }")
    except esf.GraphQLError as exc:
        assert exc.error_code == "ValidationError"
```

`on_graphql(match, data=None, *, nodes=None, path=None, page_size=10, times=None)` takes these
arguments:

- `match` is the document, a compiled regex or a predicate `(query) -> bool`, as for
  `on_query()`.
- `data` is the `data` dict, a callable `(query, variables) -> data`, or an exception to
  raise.
- `nodes` and `path` build a connection with `edges`, `pageInfo` and `totalCount`. The
  cursors are `fake-cursor-<offset>`.

The fake checks the query and `path` of `graphql_iter()` like the real client. Each page is
one `"graphql"` call in `calls`.

## Inject errors

`fail_next()` makes the next call of an operation raise. `reject()` rejects record writes row by
row.

```python
def test_validation_error_is_reported(esf_fake: FakeSalesforce) -> None:
    lead_id = esf_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
    esf_fake.fail_next("sobject.update", esf.ValidationRuleFailed("[ADDR] Street is required"))

    try:
        close_lead(esf_fake, lead_id)
    except esf.ValidationRuleFailed as exc:
        assert "Street" in exc.message
    else:
        raise AssertionError("expected ValidationRuleFailed")


def test_partial_failure(esf_fake: FakeSalesforce) -> None:
    esf_fake.reject(
        "Lead",
        esf.RequiredFieldMissing("Required fields are missing: [Company]", fields=["Company"]),
        when=lambda data: not data.get("Company"),
    )

    result = esf_fake.collections.create(
        [{"LastName": "A", "Company": "Acme"}, {"LastName": "B"}], sobject="Lead"
    )

    assert [r.ok for r in result] == [True, False]
```

`fail_next(operation, error, *, sobject=None, times=1)` uses the real operation names:

- `"sobject.update"`
- `"query"` and `"query_more"`
- `"collections.create"`
- `"actions.flow"`, `"actions.apex"` and `"actions.standard"`
- `"apex.get"`, `"apex.post"` and the other Apex REST methods
- `"limits"`
- `"graphql"`
- `"raw"`, or the name you pass as `operation=`
- `"*"` for any operation

`reject(sobject, error, *, when=None)` applies to record writes. A single write raises the error.
A Collections call returns `RecordResult(ok=False, ...)` for each rejected row. With
`all_or_none=True`, the fake rolls back the chunk, as the real API does.

## Fake actions and Apex REST

`on_action(kind, name, outputs)` answers `sf.actions.*` and `sf.run()`. The fake runs the
handler once per input. `on_apex(method, path, response)` answers `sf.apex.*`.

```python
def assign(sf, lead_id: str) -> str:  # code under test
    return sf.actions.flow("Assign_Lead", {"leadId": lead_id})["assignedTo"]


def test_assign(esf_fake: FakeSalesforce) -> None:
    def handler(inputs: dict) -> dict:
        if not inputs.get("leadId"):
            raise esf.RequiredFieldMissing("leadId is required", error_code="REQUIRED_FIELD_MISSING")
        return {"assignedTo": "005000000000001AAA"}

    esf_fake.on_action("flow", "Assign_Lead", handler)
    assert assign(esf_fake, "00Q000000000001AAA") == "005000000000001AAA"

    results = esf_fake.actions.flow("Assign_Lead", [{"leadId": "00Q000000000001AAA"}, {}])
    assert [r.ok for r in results] == [True, False]


def test_closed_order(esf_fake: FakeSalesforce) -> None:
    esf_fake.on_apex("POST", "/orders/submit", esf.ApexError("closed", status=422, body={"error": "CLOSED"}))
    try:
        esf_fake.apex.post("/orders/submit", {"orderId": "801"})
    except esf.ApexError as exc:
        assert exc.body == {"error": "CLOSED"}
    else:
        raise AssertionError("expected ApexError")
```

`outputs` is a dict, a callable that gets the inputs, or a `SalesforceError`. If the handler
raises or returns a `SalesforceError`, that input fails: a single call raises it, and a list call
reports it in its `ActionResult`. `set_action_describe(kind, name, describe)` sets what
`actions.describe()` returns. `on_apex` takes the same `response` values as `on_request`, and
its path is the part after `/services/apexrest`. A call with no handler raises `UnscriptedCall`.

## Assert on calls

`fake.calls` lists every call as a `FakeCall(operation, sobject, args, caller, priority, error)`.
`calls_for(operation, sobject=None)` filters that list. `reset_calls()` clears it.

The fake also records calls through `tagged()` and `with_auth()` views, with their `caller` and
`priority`.

Every call emits an `sf.request` event and counts in `fake.usage.snapshot()`. So `esf.capture()`,
observers and [budgets](usage-and-budgets.md#budgets) work in tests.

## Test async code

`AsyncFakeSalesforce` has the same controls. Its client methods are coroutines, and `query_iter` is
an async iterator. `fake.sync` is the sync fake underneath. Use it with pytest-asyncio, anyio or
plain `asyncio.run`.

```python
import asyncio

from even_simpler_salesforce.testing import AsyncFakeSalesforce


async def count_open(sf) -> int:  # async code under test
    return await sf.count("SELECT COUNT() FROM Lead WHERE Status = 'Open'")


def test_count_open(esf_async_fake: AsyncFakeSalesforce) -> None:
    esf_async_fake.on_query("SELECT COUNT() FROM Lead WHERE Status = 'Open'", total_size=42)
    assert asyncio.run(count_open(esf_async_fake)) == 42
```

## Record traffic

Contract tests run the real client against recorded responses. Record them once against a sandbox.
`record()` returns an httpx transport. The transport forwards each request and appends the redacted
exchange to a JSON file.

!!! warning "Review every new fixture before you commit it"
    Redaction covers the keys and patterns listed below. Other personal data in a response stays in
    the file.

```python
import httpx

from even_simpler_salesforce import testing

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
transport = testing.record("leads.json", extra_keys={"Tax_Id__c"})
with esf.Salesforce(auth, http_client=httpx.Client(transport=transport)) as sf:
    sf.query("SELECT Id, Name FROM Lead LIMIT 2")
```

The recorder redacts before it writes anything to disk:

- OAuth secrets anywhere in JSON or form bodies become `"<redacted>"`. Examples are
  `access_token`, `refresh_token`, `id_token`, `client_secret`, `assertion`, `code`,
  `code_verifier`, `signature` and `password`. Add your own keys with `extra_keys=`.
- Session ids and `Bearer ...` values inside any string are redacted.
- E-mail addresses become `user<N>@example.com`, and phone and fax values become `555-01NN`. The
  values stay consistent within a file. Turn this off with `mask_emails=` and `mask_phones=`.
- In sObject records, names and postal addresses become `<Field> <N>`. Examples are `Name`,
  `FirstName`, `LastName`, `MailingStreet`, `BillingCity` and `PostalCode`. Literals in SOQL and
  SOSL query parameters become `?`. Turn this off with `mask_personal=False`.
- Instance host names become `example.my.salesforce.com`. Turn this off with `mask_hosts=`.
- Fake ids that keep the key prefix replace Salesforce ids. The fake ids stay consistent across
  paths, query strings and bodies. Turn this off with `mask_ids=False`.
- The recorder keeps only a short allow-list of headers. It never writes `Authorization` or
  cookies.

Redaction uses patterns, so a custom field can still hold personal data. Add its API name to
`extra_keys=`, and read every new fixture before you commit it.

## Replay traffic

`replay()` serves the file through `httpx.MockTransport`. The same transport works with
`httpx.Client` and `httpx.AsyncClient`.

```python
replayed = esf.Salesforce(
    esf.StaticToken("test-token", "https://example.my.salesforce.com"),
    http_client=httpx.Client(transport=testing.replay("leads.json")),
)
page = replayed.query("SELECT Id, Name FROM Lead LIMIT 2")
assert len(page) == 2
replayed.close()
```

For each request, replay serves the first unused interaction with the same method, path and
query string. Replay compares SOQL and SOSL in `q` and `explain` with the literals replaced by
`?`, because the recording stores them that way.

| Option | Effect |
|---|---|
| `match_query=False` | match on method and path only, and serve the recordings in order |
| `repeat=True` | serve the last match again after all matches are used |

If no interaction matches, replay raises `ReplayMismatch`, which is an `AssertionError`.
`transport.handler.remaining` counts the interactions that replay hasn't served yet.

## pytest plugin

The package registers a pytest plugin automatically, through the `pytest11` entry point. It gives
you these fixtures:

| Fixture | Gives |
|---|---|
| `esf_fake` | a fresh `FakeSalesforce`, closed after the test |
| `esf_async_fake` | a fresh `AsyncFakeSalesforce` |
| `esf_replay` | factory `esf_replay(path, match_query=True, repeat=False)` returning a replay transport; relative paths resolve against the test file's directory |
| `esf_sandbox_auth` | an `Auth` built from `ESF_SANDBOX_*` variables; the test is skipped if they aren't set |

```python
def test_with_replay(esf_replay) -> None:
    transport = esf_replay("leads.json")  # next to the test file in a real suite
    with esf.Salesforce(
        esf.StaticToken("test-token", "https://example.my.salesforce.com"),
        http_client=httpx.Client(transport=transport),
    ) as sf:
        assert sf.query("SELECT Id, Name FROM Lead LIMIT 2").total_size == 2
```

## Real-org tests

Mark tests that need a real org with `@pytest.mark.esf_sandbox`. If no complete login is configured
with `ESF_SANDBOX_*` variables, pytest skips them.

```python
import pytest


@pytest.mark.esf_sandbox
def test_sandbox_smoke(esf_sandbox_auth) -> None:
    with esf.Salesforce(esf_sandbox_auth) as sf:
        assert sf.count("SELECT COUNT() FROM User") > 0
```

The variables are the same three sets as for the [CLI](cli.md#login), with the `ESF_SANDBOX_`
prefix:

| Login flow | Variables |
|---|---|
| Existing session | `ESF_SANDBOX_ACCESS_TOKEN`, `ESF_SANDBOX_INSTANCE_URL` |
| JWT bearer | `ESF_SANDBOX_DOMAIN`, `ESF_SANDBOX_CLIENT_ID`, `ESF_SANDBOX_USERNAME`, `ESF_SANDBOX_PRIVATE_KEY_FILE` |
| Client credentials | `ESF_SANDBOX_DOMAIN`, `ESF_SANDBOX_CLIENT_ID`, `ESF_SANDBOX_CLIENT_SECRET` |

Don't run these tests on pull requests from forks. They need real credentials.

## Test your token store

`TokenStoreContract` and `AsyncTokenStoreContract` check a custom store against the token store
contract. They check:

- the round trip
- versions
- compare-and-swap conflicts
- delete
- key isolation
- the lock
- exactly one winner among concurrent writers

See [Token stores](authentication/token-stores.md#checking-your-store-tokenstorecontract) for a
full example.

## Fixture format

A fixture file uses version 1 of this format:

```json
{
  "esf_fixture": 1,
  "interactions": [
    {
      "request": {"method": "GET", "path": "/services/data/v68.0/query",
                  "query": [["q", "SELECT Id FROM Lead"]], "headers": {}, "json": null},
      "response": {"status": 200,
                   "headers": {"content-type": "application/json",
                               "sforce-limit-info": "api-usage=1/15000"},
                   "json": {"totalSize": 0, "done": true, "records": []}}
    }
  ]
}
```

- A body is stored under exactly one of `json`, `form`, `text` or `base64`. A missing key means an
  empty body.
- Fixtures never store hosts.
- You can write fixtures by hand, for example for errors that are hard to trigger. If you do, mark
  them as synthetic.

## How the library tests itself

| Layer | Location | What |
|---|---|---|
| Unit | `tests/unit` | pure `core/` logic (SOQL quoting with property tests, cursors, error mapping, retry decisions, limit headers), no network |
| Contract | `tests/contract` | every public method of both clients against respx and recorded responses (`tests/fixtures/recorded`) |
| Testing module | `tests/testing_module` | the fakes, recorder, plugin and contract suite |
| Docs | `tests/docs` | runs every `python` block in these pages and the README against a synthetic org |
| Codemod | `tests/migrate` | `esf migrate`: golden before and after files per rule, CLI, idempotence and formatting property tests |
| CLI, benchmarks | `tests/cli`, `tests/benchmarks` | the `esf` command; performance (opt-in) |
| Sandbox | `tests/sandbox` | real-org smoke tests and the rotation acceptance test (opt-in) |
| Examples | `examples/*/tests` | the FastAPI and Flask example apps |

Real-org tests live in `tests/sandbox`, with the marker `sandbox`. They are skipped unless
`ESF_SANDBOX_*` variables are set. `tests/sandbox/README.md` explains the setup. It also covers the
refresh-token rotation test with 4 processes, and `scripts/verify_sandbox.py`, which records
evidence for the open verification items in the spec.
