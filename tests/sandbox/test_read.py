"""Read-only smoke tests against a real org, on both clients (no records are written).

Covers: identity (userinfo / token ids), ``limits()`` and ``Sforce-Limit-Info`` usage
tracking, ``describe_global`` / ``describe`` and describe-cache revalidation (304, V14),
query / query_more / query_iter / count / search, and error classification for a real
``INVALID_FIELD``, ``MALFORMED_QUERY``, ``NOT_FOUND`` and ``INVALID_TYPE``.

Docs: REST API Developer Guide, *Status Codes and Error Responses*
(https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/errorcodes.htm) and
*sObject Describe* (https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_describe.htm).
"""

from __future__ import annotations

import pytest

import even_simpler_salesforce as esf

from .conftest import Driver

pytestmark = pytest.mark.sandbox


def test_whoami(sb: Driver) -> None:
    sf = sb.client()
    info = sb.run(sf.auth.userinfo())
    assert info.get("user_id") or info.get("sub")
    tok = sb.run(sf.auth.token())
    assert tok.instance_url.startswith("https://")
    if tok.org_id is not None:  # StaticToken has no id URL
        assert tok.org_id.startswith("00D")
        assert info.get("organization_id", tok.org_id)[:15] == tok.org_id[:15]


def test_limits_and_usage_header(sb: Driver) -> None:
    sf = sb.client()
    limits = sb.run(sf.limits())
    daily = limits["DailyApiRequests"]
    assert daily.max > 0
    assert 0 <= daily.remaining <= daily.max
    sb.run(sf.query("SELECT Id FROM User LIMIT 1"))
    snap = sf.usage.snapshot()
    assert snap.api_max is not None
    assert snap.api_max > 0
    assert snap.api_used is not None
    assert snap.calls_by_operation.get("query", 0) >= 1
    query_events = sb.recorder.requests("query")
    assert query_events
    assert query_events[-1]["api_max"] == snap.api_max  # parsed from Sforce-Limit-Info


def test_describe_global_and_sobject(sb: Driver) -> None:
    sf = sb.client()
    glob = sb.run(sf.describe_global())
    names = {o["name"] for o in glob["sobjects"]}
    assert {"Lead", "User"} <= names
    lead = sb.run(sf.sobject("Lead").describe())
    assert lead["name"] == "Lead"
    assert "LastName" in {f["name"] for f in lead["fields"]}
    # Served from cache the second time: no new request.
    before = len(sb.recorder.requests())
    assert sb.run(sf.sobject("Lead").describe()) == lead
    assert len(sb.recorder.requests()) == before


def test_describe_revalidation_returns_304(sb: Driver) -> None:
    """With ttl=0 every describe revalidates with If-Modified-Since; Salesforce answers 304 (V14)."""
    sf = sb.client(describe_cache=esf.DescribeCacheConfig(ttl=0))
    first = sb.run(sf.sobject("Lead").describe())
    second = sb.run(sf.sobject("Lead").describe())
    assert second == first
    statuses = [e["status"] for e in sb.recorder.requests("sobject.describe")]
    assert statuses == [200, 304], statuses
    sb.run(sf.describe_global())
    sb.run(sf.describe_global())
    glob = [e["status"] for e in sb.recorder.requests("describe_global")]
    assert glob[0] == 200
    assert glob[1] in (200, 304), glob  # recorded for evidence; global describe may not honour it


def test_query_count_and_iter(sb: Driver) -> None:
    sf = sb.client()
    total = sb.run(sf.count("SELECT COUNT() FROM User"))
    assert total >= 1
    page = sb.run(sf.query("SELECT Id, Name FROM User ORDER BY Id LIMIT 5"))
    assert 1 <= len(page.records) <= 5
    assert all("Id" in r for r in page.records)
    rows = sb.collect(sf.query_iter("SELECT Id FROM User ORDER BY Id LIMIT 5"))
    assert [r["Id"] for r in rows] == [r["Id"] for r in page.records]


def test_query_more_with_smallest_page(sb: Driver) -> None:
    """``query_more`` follows a cursor when the org has more than 200 rows of some object."""
    sf = sb.client()
    for obj in ("User", "Lead", "Contact", "Account", "PermissionSet", "Profile"):
        n = sb.run(sf.count(f"SELECT COUNT() FROM {obj}"))
        if n > 200:
            break
    else:
        pytest.skip("sandbox: no object with >200 rows to page through (the collections test covers it)")
    page = sb.run(sf.query(f"SELECT Id FROM {obj}", page_size=200))
    assert page.cursor is not None
    assert not page.done
    nxt = sb.run(sf.query_more(page.cursor))
    assert nxt.records
    assert {r["Id"] for r in page.records}.isdisjoint(r["Id"] for r in nxt.records)
    rows = sb.collect(sf.query_iter(f"SELECT Id FROM {obj}", page_size=200), limit=n)
    assert len(rows) == n
    assert len(sb.recorder.requests("query_more")) >= 2


def test_search(sb: Driver) -> None:
    sf = sb.client()
    found = sb.run(sf.search("FIND {esfsandboxnomatch} IN ALL FIELDS RETURNING Lead(Id) LIMIT 1"))
    assert isinstance(found, list)


def test_invalid_field_is_classified(sb: Driver) -> None:
    sf = sb.client()
    with pytest.raises(esf.InvalidField) as ei:
        sb.run(sf.query("SELECT EsfNoSuchField__c FROM Lead LIMIT 1"))
    assert ei.value.error_code == "INVALID_FIELD"
    assert ei.value.status == 400
    assert sb.recorder.requests("query")[-1]["error_code"] == "INVALID_FIELD"


def test_malformed_query_is_classified(sb: Driver) -> None:
    sf = sb.client()
    with pytest.raises(esf.MalformedQuery) as ei:
        sb.run(sf.query("SELECT Id FROM Lead WHERE"))
    assert ei.value.error_code == "MALFORMED_QUERY"
    assert ei.value.status == 400


def test_not_found_and_invalid_type(sb: Driver) -> None:
    sf = sb.client()
    with pytest.raises(esf.NotFound) as nf:
        sb.run(sf.sobject("Lead").get("00Q000000000000AAA"))
    assert nf.value.status == 404
    with pytest.raises((esf.InvalidType, esf.NotFound)) as it:
        sb.run(sf.sobject("EsfNoSuchObject__c").describe(cached=False))
    assert it.value.status == 404
