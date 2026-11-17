---
name: esf-testing
description: >-
  Write tests for code that uses even-simpler-salesforce without a real org: the
  even_simpler_salesforce.testing fakes (FakeSalesforce, AsyncFakeSalesforce) with scripted
  queries and injected errors, the pytest fixtures (esf_fake, esf_async_fake, esf_replay,
  esf_sandbox_auth), and record and replay of redacted traffic. Use it when the user asks to
  "test Salesforce code", "mock Salesforce", "write a unit test for this sync job", "record a
  fixture", "replay Salesforce responses" or "replace MagicMock on sf.Lead".
---

# Test code that uses the client

Use the fakes for unit tests of your own logic. Use record and replay for contract tests of how
your code talks to Salesforce. Don't mock the client with `MagicMock`, and don't mock httpx.

| Tool | Use it for | Network |
|---|---|---|
| `FakeSalesforce`, `AsyncFakeSalesforce` | unit tests: in-memory records, scripted queries, injected errors | none |
| `testing.record()`, `testing.replay()` | contract tests: capture real traffic once, redacted, then replay it | only while you record |
| pytest plugin | fixtures `esf_fake`, `esf_async_fake`, `esf_replay`, `esf_sandbox_auth` and the marker `esf_sandbox` | none |

The plugin installs with the package. The `[testing]` extra adds pytest and respx.

## Use the fakes

Pass the fake where your code expects a client. It has the client's methods and stores records
per sObject.

```python
import re

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import FakeSalesforce


def close_lead(sf, lead_id: str) -> None:  # the code under test
    sf.sobject("Lead").update(lead_id, {"Status": "Closed - Not Converted"})


def open_lead_ids(sf) -> list[str]:  # the code under test
    return [r["Id"] for r in sf.query_iter("SELECT Id FROM Lead WHERE Status = {}", "Open")]


def test_close_lead(esf_fake: FakeSalesforce) -> None:
    lead_id = esf_fake.add("Lead", {"LastName": "Doe", "Company": "Acme", "Status": "Open"})

    close_lead(esf_fake, lead_id)

    assert esf_fake.record("Lead", lead_id)["Status"] == "Closed - Not Converted"
    [call] = esf_fake.calls_for("sobject.update", sobject="Lead")
    assert call.args["data"] == {"Status": "Closed - Not Converted"}


def test_open_leads(esf_fake: FakeSalesforce) -> None:
    ids = esf_fake.add_many("Lead", [{"LastName": f"L{i}", "Company": "Acme"} for i in range(3)])
    esf_fake.on_query(
        re.compile(r"FROM Lead WHERE Status = 'Open'"), rows=lambda soql: esf_fake.records("Lead")
    )

    assert open_lead_ids(esf_fake) == ids
```

- The fake has no SOQL engine. Script every query with `on_query(match, rows=...)`. `match` is
  the SOQL text, a compiled regex or a predicate. The newest matching script wins.
- An unscripted query, search or request raises `UnscriptedCall`. It is an `AssertionError`, so
  `except esf.SalesforceError` in your code can't hide it.
- The fake doesn't run validation rules, triggers or required-field checks. Inject them.
- For async code, use `esf_async_fake` or `AsyncFakeSalesforce()`. It has the same controls.
- The fakes are duck-typed. If pyright checks your function against `esf.Salesforce`, use
  `typing.cast`.

## Inject errors

```python
def test_validation_error(esf_fake: FakeSalesforce) -> None:
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

`fail_next(operation, error)` uses the operation names of the real client, such as
`"sobject.update"`, `"query"`, `"collections.create"`, `"actions.flow"`, `"limits"` or `"*"`.
`reject(sobject, error, when=...)` fails record writes row by row.

## Record and replay

Record once against a sandbox, never production. `record()` returns an httpx transport that
redacts each exchange and appends it to a JSON file.

```python
import httpx

from even_simpler_salesforce import testing

auth = esf.ClientCredentials(
    domain="https://acme.my.salesforce.com", client_id="3MVG9...", client_secret="..."
)
transport = testing.record("leads.json", extra_keys={"Tax_Id__c"})
with esf.Salesforce(auth, http_client=httpx.Client(transport=transport)) as sf:
    sf.query("SELECT Id, Name FROM Lead LIMIT 2")


def test_with_replay(esf_replay) -> None:
    with esf.Salesforce(
        esf.StaticToken("test-token", "https://example.my.salesforce.com"),
        http_client=httpx.Client(transport=esf_replay("leads.json")),
    ) as sf:
        assert sf.query("SELECT Id, Name FROM Lead LIMIT 2").total_size == 2
```

The recorder redacts OAuth secrets, session ids, bearer values, e-mail addresses, phone
numbers, names, postal addresses, SOQL literals, host names and record ids. It keeps only a
short list of headers.

**Read every fixture before you commit it.** Redaction uses patterns, so a custom field can
still hold personal data. Add such fields to `extra_keys=`, record again, and read the file
again. Never commit a value that matches `00D...!`, which is an access token. If you write a
fixture by hand, mark it as synthetic in the fixture and in the test.

Replay matches the method, path and query string, with SOQL literals replaced by `?`. A
request that matches nothing raises `ReplayMismatch`.

## Tests against a real org

Mark them with `@pytest.mark.esf_sandbox` and use the `esf_sandbox_auth` fixture. pytest skips
them unless `ESF_SANDBOX_*` variables hold a complete login. Don't run them on pull requests
from forks.

```python
import pytest


@pytest.mark.esf_sandbox
def test_sandbox_smoke(esf_sandbox_auth) -> None:
    with esf.Salesforce(esf_sandbox_auth) as sf:
        assert sf.count("SELECT COUNT() FROM User") > 0
```

## More

- Testing guide: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/testing.md
- Index of all docs pages: https://raw.githubusercontent.com/Uttsavv/even-simpler-salesforce/main/docs/llms.txt
