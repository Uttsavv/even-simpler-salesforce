"""Regression tests for the core API / security review of 0.1.0rc1 (both clients)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce._async.auth_runtime import key_hash
from even_simpler_salesforce._async.client import _sobject_of  # pyright: ignore[reportPrivateUsage]
from even_simpler_salesforce._async.transport import LruDict

from ..conftest import DATA, INSTANCE, Harness, Recorder, make_token, web_auth

DESCRIBE = {"name": "Lead", "fields": [{"name": "Id"}, {"name": "Email"}]}


# --- sobject label of queries with subqueries ---------------------------------------------------


@pytest.mark.parametrize(
    ("soql", "sobject"),
    [
        ("SELECT Id FROM Lead", "Lead"),
        ("SELECT Id, (SELECT Id FROM Contacts) FROM Account", "Account"),
        ("SELECT Id, (SELECT Id, (SELECT Id FROM X) FROM Contacts) FROM Account WHERE A = 1", "Account"),
        ("SELECT Id FROM Account WHERE Id IN (SELECT AccountId FROM Contact)", "Account"),
        ("SELECT Id FROM Lead WHERE Name = 'x) FROM Evil'", "Lead"),
        ("SELECT COUNT()", None),
    ],
)
def test_sobject_of_skips_subqueries(soql: str, sobject: str | None) -> None:
    assert _sobject_of(soql) == sobject


def test_query_one_reports_outer_object(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/query").respond(json={"totalSize": 0, "done": True, "records": []})
    sf = h.client()
    with pytest.raises(esf.NotFound) as ei:
        h.run(sf.query_one("SELECT Id, (SELECT Id FROM Contacts) FROM Account"))
    assert ei.value.sobject == "Account"


# --- describe cache -----------------------------------------------------------------------------


def test_describe_returns_private_copies(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/sobjects/Lead/describe").respond(json=DESCRIBE)
    sf = h.client()
    first = h.run(sf.sobject("Lead").describe())
    first["fields"].clear()  # a caller mutating its result must not poison the cache
    second = h.run(sf.sobject("Lead").describe())
    assert second == DESCRIBE
    assert route.call_count == 1
    assert h.run(sf.sobject("Lead").describe(cached=False)) == DESCRIBE


def test_describe_cache_miss_is_single_flight(mock: respx.MockRouter) -> None:
    from ..conftest import Harness as _H

    h = _H("async")
    try:
        route = mock.get(f"{DATA}/sobjects/Lead/describe").respond(json=DESCRIBE)
        sf = h.client()

        async def go() -> list[Any]:
            return await asyncio.gather(*(sf.sobject("Lead").describe() for _ in range(10)))

        results = h.run(go())
        assert all(r == DESCRIBE for r in results)
        assert route.call_count == 1
    finally:
        h.close()


# --- per-identity state is bounded and cleaned up -----------------------------------------------


def test_lru_dict_bounds_and_recency() -> None:
    d: LruDict[str, int] = LruDict(2)
    d["a"] = 1
    d["b"] = 2
    assert d.get("a") == 1  # "a" is now the most recent
    d["c"] = 3
    assert list(d) == ["a", "c"]
    assert d.get("missing", 0) == 0


def test_revoke_drops_refresh_outcome(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{'https://acme.my.salesforce.com'}/services/oauth2/revoke").respond(200)
    auth = web_auth()
    sf = h.client(auth, token=make_token("AT", "RT"))
    h.run(sf.auth.token())
    sf._transport.refresh_outcomes[auth.token_key] = (1, None)
    h.run(sf.auth.revoke())
    assert auth.token_key not in sf._transport.tokens
    assert auth.token_key not in sf._transport.refresh_outcomes


def test_evicted_token_is_reloaded_from_store(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/limits").respond(json={})
    sf = h.client()
    sf._transport.tokens.maxsize = 1
    h.run(sf.limits())
    other = sf.with_auth(esf.StaticToken("00D!other", INSTANCE))
    h.run(other.limits())  # evicts the first client's in-memory token
    assert sf._runtime.key not in sf._transport.tokens
    h.run(sf.limits())  # reloaded from the store, no new login
    assert route.call_count == 3
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {make_token().access_token}"


def test_runtime_key_hash_follows_key(h: Harness) -> None:
    sf = h.client(web_auth())
    rt = sf.auth._runtime_for("user:42", None)
    assert rt.key == "user:42"
    assert rt.key_hash == key_hash("user:42")


# --- BatchResult.succeeded ----------------------------------------------------------------------


def test_batch_ok_is_deprecated() -> None:
    ok = esf.RecordResult(ok=True, id="001")
    bad = esf.RecordResult(ok=False)
    batch = esf.BatchResult([ok, bad])
    assert batch.succeeded == [ok]
    with pytest.warns(esf.EsfDeprecationWarning, match="succeeded"):
        assert batch.ok == [ok]


# --- https only for bearer tokens ---------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://acme.my.salesforce.com", "acme.my.salesforce.com", "ftp://x", ""])
def test_static_token_requires_https(url: str) -> None:
    with pytest.raises(ValueError, match="https://"):
        esf.StaticToken("00D!x", url)


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1", "http://[::1]:9000/"])
def test_static_token_allows_plain_http_to_loopback(url: str) -> None:
    assert esf.StaticToken("00D!x", url).instance_url == url.rstrip("/")


def test_instance_url_override_requires_https(h: Harness) -> None:
    with pytest.raises(ValueError, match="https://"):
        h.client(instance_url="http://acme.my.salesforce.com")
    sf = h.client()
    with pytest.raises(ValueError, match="https://"):
        sf.with_auth(web_auth(), instance_url="http://acme.my.salesforce.com")


# --- httpx errors are wrapped -------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.DecodingError("bad gzip"),
        httpx.TooManyRedirects("loop"),
    ],
)
def test_httpx_request_errors_become_transport_errors(
    h: Harness, mock: respx.MockRouter, exc: Exception
) -> None:
    mock.get(f"{DATA}/limits").mock(side_effect=exc)
    sf = h.client()
    with pytest.raises(esf.TransportError):
        h.run(sf.limits())


def test_transport_error_event_outcome(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{DATA}/limits").mock(side_effect=httpx.DecodingError("bad gzip"))
    sf = h.client(recorder=rec)
    with pytest.raises(esf.TransportError):
        h.run(sf.limits())
    assert rec.requests[-1].fields.get("error_class") == "TransportError"


# --- SOSL escaping ------------------------------------------------------------------------------


def test_sosl_term_in_search(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/search").respond(json={"searchRecords": []})
    sf = h.client()
    term = esf.sosl_term("x} IN ALL FIELDS RETURNING User(Id) //")
    h.run(sf.search(f"FIND {term} IN NAME FIELDS RETURNING Lead(Id)"))
    q = route.calls.last.request.url.params["q"]
    assert q == "FIND {x\\} IN ALL FIELDS RETURNING User\\(Id\\) //} IN NAME FIELDS RETURNING Lead(Id)"
