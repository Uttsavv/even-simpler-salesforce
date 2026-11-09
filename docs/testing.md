# Testing

`even_simpler_salesforce.testing` gives you three ways to test code that uses the client
without a real org:

| Tool | Use it for | Network |
|---|---|---|
| `FakeSalesforce` / `AsyncFakeSalesforce` | unit tests of your own logic: an in-memory org with scripted queries and injected errors | none |
| `record()` / `replay()` | contract tests: capture real traffic once (redacted), then replay it through the real client | only while recording |
| pytest plugin | fixtures for the above, plus a marker for opt-in real-org tests | — |

`import even_simpler_salesforce.testing` needs neither pytest nor respx. The pytest plugin and
the token-store contract suite need pytest (the `[testing]` extra installs pytest and respx).

## The fakes

`FakeSalesforce` (sync) and `AsyncFakeSalesforce` (async) have the real clients' methods for
records, query and search, Collections, `request`, `limits`, `usage`, `with_auth` and
`tagged`, and they share one implementation. They are duck-typed, not subclasses, so use
`typing.cast` where your code is type-checked against the real class.

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

### Records

Records are stored per sObject. `create` returns valid-looking 18-character ids with the right
key prefix (`00Q` for Lead, `001` for Account, ...), and `get`, `update`, `upsert`, `delete`
and the Collections methods behave like the API: `NotFound` for missing ids,
`NotFound(ENTITY_IS_DELETED)` for a second delete, `MultipleMatches` for ambiguous upserts and
`Conflict` for `if_unmodified_since`.

| Control | Does |
|---|---|
| `fake.add(sobject, data)` / `add_many(sobject, rows)` | seed records without recording a call; returns the id(s) |
| `fake.records(sobject)` / `fake.record(sobject, id)` | copies of what is stored |
| `fake.new_id(sobject)` | a fresh id with the sObject's prefix |
| `fake.set_describe(sobject, describe)` | what `describe()` returns (a minimal one is made up otherwise) |
| `fake.set_limits({...})` | what `limits()` returns |
| `fake.set_api_usage(used, max)` | pretend a `Sforce-Limit-Info` header was seen (drives budgets) |

The fake does **not** run validation rules, triggers or required-field checks. To simulate
them, use `reject()` or `fail_next()` (below).

### Queries are scripted, not executed

The fake has no SOQL engine, because a fake engine would give answers your org wouldn't. You
script the answers instead:

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

- `on_query(match, rows=(), *, total_size=None, times=None)`: `match` is the SOQL text
  (whitespace is collapsed before comparing), a compiled regex (`re.search`) or a predicate
  `(soql) -> bool`. `rows` is a list or a callable. `total_size` defaults to the number of rows,
  and is what `count()` returns. The newest matching script wins. `times` limits how often the
  script may match.
- `on_search(match, rows=(), *, times=None)`: the same for `search()` and
  `parameterized_search()` (matched on `q`).
- `on_request(method, path, response=None, *, times=None)`: answers `sf.request(...)`. `path`
  is a string or a regex (`fullmatch`); a versioned `/services/data/vXX.X/` prefix is ignored.
  `response` is a value (deep-copied), a callable receiving a `FakeRequest`, or an exception to
  raise.
- A query, search or request with no matching script raises `UnscriptedCall`. It is an
  `AssertionError`, so `except esf.SalesforceError` in your code can't swallow it.

Pages honour `page_size` (default 2,000), and cursors work, including signing with
`FakeSalesforce(cursor_key=...)`. `query_iter(..., expand_children=True)` is accepted but does
nothing: script child records already complete.

### Errors

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

- `fail_next(operation, error, *, sobject=None, times=1)` makes the next call(s) of an
  operation raise. Operation names are the real ones: `"sobject.update"`, `"query"`,
  `"query_more"`, `"collections.create"`, `"limits"`, `"raw"` (or your `operation=`), or `"*"`
  for any.
- `reject(sobject, error, *, when=None)` rejects matching **record writes**, row by row. Single
  writes raise the error, and Collections calls return `RecordResult(ok=False, ...)` for the
  rejected rows. With `all_or_none=True`, the chunk is rolled back, as in the real API.

### Assertions on calls

`fake.calls` lists every call as a `FakeCall(operation, sobject, args, caller, priority,
error)`. `calls_for(operation, sobject=None)` filters that list, and `reset_calls()` clears it.
Calls through `tagged()` and `with_auth()` views are recorded on the same fake, with their
`caller` and `priority`.

Every call also emits an `sf.request` event and counts in `fake.usage.snapshot()`, so
`esf.capture()`, observers and [budgets](usage-and-budgets.md#budgets) work in tests.

### The async fake

`AsyncFakeSalesforce` has the same controls. Its client methods are coroutines and
`query_iter` is an async iterator. `fake.sync` is the underlying sync fake. Use it with
pytest-asyncio, anyio, or plain `asyncio.run`:

```python
import asyncio

from even_simpler_salesforce.testing import AsyncFakeSalesforce


async def count_open(sf) -> int:  # async code under test
    return await sf.count("SELECT COUNT() FROM Lead WHERE Status = 'Open'")


def test_count_open(esf_async_fake: AsyncFakeSalesforce) -> None:
    esf_async_fake.on_query("SELECT COUNT() FROM Lead WHERE Status = 'Open'", total_size=42)
    assert asyncio.run(count_open(esf_async_fake)) == 42
```

## Record and replay

Contract tests run the **real** client against recorded responses. Record once against a
sandbox. `record()` returns an httpx transport that forwards each request and appends the
redacted exchange to a JSON file:

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

Redaction happens **before anything is written to disk**:

- OAuth secrets anywhere in JSON or form bodies (`access_token`, `refresh_token`, `id_token`,
  `client_secret`, `assertion`, `code`, `code_verifier`, `signature`, `password`, ...) become
  `"<redacted>"`. Add your own keys with `extra_keys=`;
- session ids and `Bearer ...` values inside any string are redacted;
- e-mail addresses become `user<N>@example.com` and phone/fax values `555-01NN`, consistently
  within a file (`mask_emails=` / `mask_phones=`);
- in sObject records, names and postal addresses (`Name`, `FirstName`, `LastName`,
  `MailingStreet`, `BillingCity`, `PostalCode`, ...) become `<Field> <N>`, consistently within
  a file, and literals in SOQL/SOSL query parameters become `?` (`mask_personal=`);
- instance host names become `example.my.salesforce.com` (`mask_hosts=`);
- Salesforce ids are replaced with fake ids that keep the key prefix, consistently across
  paths, query strings and bodies (`mask_ids=False` keeps the real ids);
- only a short allow-list of headers is kept; `Authorization` and cookies are never written.

Redaction is pattern-based, so a custom field can still hold personal data. Pass its API name
in `extra_keys=` and review every new fixture before you commit it.

Replay serves the file through `httpx.MockTransport`. The same transport works with
`httpx.Client` and `httpx.AsyncClient`:

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
query string. SOQL and SOSL in `q` and `explain` are compared with literals replaced by `?`,
because recordings store them that way. `match_query=False` matches on method and path only
and serves recordings in order. With `repeat=True`, the last match is
served again when all matches have been used. A request with no match raises
`ReplayMismatch`, which is an `AssertionError`. `transport.handler.remaining` counts the
interactions not served yet.

??? info "Fixture format (version 1)"
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

    A body is stored under exactly one of `json`, `form`, `text` or `base64`. A missing key
    means an empty body. Hosts are never stored. You may write fixtures by hand, for example for
    errors that are hard to trigger. If you do, mark them as synthetic.

## pytest plugin

Installing the package registers a pytest plugin (entry point `pytest11`) automatically:

| Fixture | Gives |
|---|---|
| `esf_fake` | a fresh `FakeSalesforce`, closed after the test |
| `esf_async_fake` | a fresh `AsyncFakeSalesforce` |
| `esf_replay` | factory `esf_replay(path, match_query=True, repeat=False)` returning a replay transport; relative paths resolve against the test file's directory |
| `esf_sandbox_auth` | an `Auth` built from `ESF_SANDBOX_*` variables (the test is skipped if they aren't set) |

```python
def test_with_replay(esf_replay) -> None:
    transport = esf_replay("leads.json")  # next to the test file in a real suite
    with esf.Salesforce(
        esf.StaticToken("test-token", "https://example.my.salesforce.com"),
        http_client=httpx.Client(transport=transport),
    ) as sf:
        assert sf.query("SELECT Id, Name FROM Lead LIMIT 2").total_size == 2
```

### Real-org tests: `@pytest.mark.esf_sandbox`

Mark tests that need a real org. They are **skipped** unless a complete login is configured with
`ESF_SANDBOX_*` variables. These use the same three sets as the [CLI](cli.md#login), with the
`ESF_SANDBOX_` prefix:

- `ESF_SANDBOX_ACCESS_TOKEN` + `ESF_SANDBOX_INSTANCE_URL`, or
- `ESF_SANDBOX_DOMAIN` + `ESF_SANDBOX_CLIENT_ID` + `ESF_SANDBOX_USERNAME` + `ESF_SANDBOX_PRIVATE_KEY_FILE`, or
- `ESF_SANDBOX_DOMAIN` + `ESF_SANDBOX_CLIENT_ID` + `ESF_SANDBOX_CLIENT_SECRET`.

```python
import pytest


@pytest.mark.esf_sandbox
def test_sandbox_smoke(esf_sandbox_auth) -> None:
    with esf.Salesforce(esf_sandbox_auth) as sf:
        assert sf.count("SELECT COUNT() FROM User") > 0
```

Never run these on pull requests from forks, because they need real credentials.

## Testing your token store

`TokenStoreContract` and `AsyncTokenStoreContract` check a custom store against the token-store
contract (round trip, versions, compare-and-swap conflicts, delete, key isolation, the lock, and
exactly one winner among concurrent writers). See
[Token stores](authentication/token-stores.md#checking-your-store-tokenstorecontract) for a full
example.

## How the library tests itself

| Layer | Location | What |
|---|---|---|
| Unit | `tests/unit` | pure `core/` logic (SOQL quoting with property tests, cursors, error mapping, retry decisions, limit headers), no network |
| Contract | `tests/contract` | every public method of **both** clients against respx and recorded responses (`tests/fixtures/recorded`) |
| Testing module | `tests/testing_module` | the fakes, recorder, plugin and contract suite |
| Docs | `tests/docs` | runs every `python` block in these pages and the README against a synthetic org |
| Codemod | `tests/migrate` | `esf migrate`: golden before/after files per rule, CLI, idempotence and formatting property tests |
| CLI, benchmarks | `tests/cli`, `tests/benchmarks` | the `esf` command; performance (opt-in) |
| Sandbox | `tests/sandbox` | real-org smoke tests and the rotation acceptance test (opt-in) |
| Examples | `examples/*/tests` | the FastAPI and Flask example apps |

Real-org tests live in `tests/sandbox` (marker `sandbox`). They are skipped unless
`ESF_SANDBOX_*` variables are set; `tests/sandbox/README.md` explains the setup, including the
4-process refresh-token rotation test and `scripts/verify_sandbox.py`, which records evidence for
the spec's open verification items.
