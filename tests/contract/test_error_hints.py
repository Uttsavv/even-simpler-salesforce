"""Error hints and docs links (spec §8.5), on both clients.

Every Salesforce response here is ``synthetic`` (hand-written; see ``tests/fixtures``). Sources:

- error codes and statuses: *Status Codes and Error Responses*, REST API Developer Guide,
  https://developer.salesforce.com/docs/platform/api-rest/guide/errorcodes.html
- API access needs an edition with API access and the "API Enabled" user permission:
  *Supported Editions and Required Permissions*,
  https://developer.salesforce.com/docs/platform/api-rest/guide/intro-rest-compatible-editions.html
- the ``api`` OAuth scope ("Manage user data via APIs") grants REST API access: *Scope Parameter
  Values*, https://developer.salesforce.com/docs/platform/mobile-sdk/guide/oauth-scope-parameter-values.html
- SOQL quote escaping: *Quoted String Escape Sequences*,
  https://developer.salesforce.com/docs/atlas.en-us.soql_sosl.meta/soql_sosl/sforce_api_calls_soql_select_quotedstringescapes.htm
"""

from __future__ import annotations

import pickle

import httpx
import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce.errors import ERRORS_PAGE

from ..conftest import DATA, TOKEN_URL, Harness, token_body
from ..fixtures import load_fixture

QUERY = f"{DATA}/query"
DESCRIBE = f"{DATA}/sobjects/Lead/describe"


def no_such_column(field: str) -> httpx.Response:
    message = (
        f"\nSELECT {field} FROM Lead\n       ^\nERROR at Row:1:Column:8\n"
        f"No such column '{field}' on entity 'Lead'."
    )
    return httpx.Response(400, json=[{"errorCode": "INVALID_FIELD", "message": message}])


def test_invalid_field_suggests_a_field_from_the_cached_describe(h: Harness, mock: respx.MockRouter) -> None:
    describe = mock.get(DESCRIBE).respond(json=load_fixture("sobject/describe_lead_fields.json"))
    mock.get(QUERY).mock(return_value=no_such_column("Emial"))
    sf = h.client()
    h.run(sf.sobject("Lead").describe())
    with pytest.raises(esf.InvalidField) as info:
        h.run(sf.query("SELECT Emial FROM Lead"))
    exc = info.value
    assert exc.hint == "did you mean 'Email'?"
    assert exc.docs_url == f"{ERRORS_PAGE}#invalidfield"
    assert str(exc).endswith(f"\n  hint: did you mean 'Email'?\n  docs: {ERRORS_PAGE}#invalidfield")
    assert describe.call_count == 1  # building the hint made no API call
    clone = pickle.loads(pickle.dumps(exc))
    assert (clone.hint, clone.docs_url, str(clone)) == (exc.hint, exc.docs_url, str(exc))


def test_invalid_field_without_a_cached_describe_has_no_hint(h: Harness, mock: respx.MockRouter) -> None:
    describe = mock.get(DESCRIBE).respond(json=load_fixture("sobject/describe_lead_fields.json"))
    mock.post(f"{DATA}/sobjects/Lead/").respond(
        400,
        json=[
            {
                "errorCode": "INVALID_FIELD",
                "message": "No such column 'Emial' on sobject of type Lead",
                "fields": ["Emial"],
            }
        ],
    )
    sf = h.client()
    with pytest.raises(esf.InvalidField) as info:
        h.run(
            sf.sobject("Lead").create({"LastName": "Curie", "Company": "Sorbonne", "Emial": "m@example.com"})
        )
    assert info.value.hint is None
    assert info.value.docs_url == f"{ERRORS_PAGE}#invalidfield"
    assert not describe.called  # never fetched just for a hint


def test_session_rejected_after_refresh_points_at_the_connected_app(
    h: Harness, mock: respx.MockRouter
) -> None:
    mock.post(TOKEN_URL).respond(json=token_body("AT-fresh-0002"))
    mock.get(f"{DATA}/limits").respond(401, json=load_fixture("errors/invalid_session.json"))
    sf = h.client()
    with pytest.raises(esf.SessionExpired) as info:
        h.run(sf.limits())
    hint = info.value.hint or ""
    assert "'api' OAuth scope" in hint
    assert "AT-" not in hint
    assert "AT-" not in str(info.value)
    assert info.value.docs_url == f"{ERRORS_PAGE}#sessionexpired"


def test_api_disabled_hint(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/limits").respond(403, json=load_fixture("errors/api_disabled.json"))
    with pytest.raises(esf.Forbidden) as info:
        h.run(h.client().limits())
    assert "'API Enabled' permission" in (info.value.hint or "")
    assert info.value.docs_url == f"{ERRORS_PAGE}#forbidden"


def test_request_limit_hint_links_to_usage(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/limits").respond(403, json=load_fixture("errors/request_limit.json"))
    with pytest.raises(esf.RequestLimitExceeded) as info:
        h.run(h.client().limits())
    assert "sf.usage.snapshot()" in (info.value.hint or "")
    assert "usage-and-budgets" in (info.value.hint or "")


def test_unescaped_quote_suggests_query_values(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(QUERY).respond(400, json=load_fixture("errors/malformed_query_quote.json"))
    sf = h.client()
    with pytest.raises(esf.MalformedQuery) as info:
        h.run(sf.query("SELECT Id FROM Lead WHERE LastName = 'O'Brien'"))
    hint = info.value.hint or ""
    assert "sf.query(" in hint
    assert "Brien" not in hint  # the hint never quotes record values
    # The same name passed as a value is quoted safely, so a MALFORMED_QUERY gets no quote hint.
    with pytest.raises(esf.MalformedQuery) as info:
        h.run(sf.query("SELECT Id FROM Lead WHERE LastName = {}", "O'Brien"))
    assert info.value.hint is None


def test_transport_errors_get_a_docs_url_too(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/limits").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(esf.ConnectError) as info:
        h.run(h.client(retry=esf.RetryPolicy(max_attempts=1)).limits())
    assert info.value.hint is None
    assert info.value.docs_url == f"{ERRORS_PAGE}#connecterror"


def test_cached_describe_before_any_token_is_none(h: Harness) -> None:
    sf = h.client(token=False)
    assert sf._transport.cached_describe(sf._runtime, "Lead") is None  # no token yet: no cache key
