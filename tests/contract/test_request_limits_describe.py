from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, Harness, Recorder
from ..fixtures import load_fixture

# --- sf.request ---------------------------------------------------------------------------------


def test_request_relative_and_absolute(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    rel = mock.get(f"{DATA}/sobjects/Lead/describe/layouts").respond(json={"layouts": []})
    apex = mock.post(f"{INSTANCE}/services/apexrest/orders/submit").respond(json={"ok": 1})
    sf = h.client(recorder=rec)
    assert h.run(sf.request("get", "sobjects/Lead/describe/layouts", params={"a": "1"})) == {"layouts": []}
    assert rel.calls.last.request.url.params["a"] == "1"
    out = h.run(
        sf.request("POST", "/services/apexrest/orders/submit", json={"orderId": "1"}, operation="apex.post")
    )
    assert out == {"ok": 1}
    req = apex.calls.last.request
    assert json.loads(req.content) == {"orderId": "1"}
    assert req.headers["Content-Type"] == "application/json"
    assert [e["operation"] for e in rec.requests] == ["raw", "apex.post"]
    assert rec.requests[1]["path_template"] == "/services/apexrest/orders/submit"


def test_request_bodies(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.put(f"{DATA}/x")
    route.side_effect = [
        httpx.Response(204),
        httpx.Response(200, text="plain text", headers={"content-type": "text/plain"}),
        httpx.Response(200, content=b"\xff\xfe", headers={"content-type": "application/octet-stream"}),
    ]
    sf = h.client()
    assert h.run(sf.request("PUT", "x", content=b"raw-bytes", headers={"Content-Type": "text/csv"})) is None
    req = route.calls.last.request
    assert req.content == b"raw-bytes"
    assert req.headers["Content-Type"] == "text/csv"
    assert h.run(sf.request("PUT", "x", content=b"abc")) == "plain text"
    assert route.calls.last.request.headers["Content-Type"] == "application/octet-stream"
    assert h.run(sf.request("PUT", "x")) == "��"
    with pytest.raises(ValueError, match="not both"):
        h.run(sf.request("PUT", "x", json={}, content=b"x"))


@pytest.mark.parametrize(
    "path",
    ["https://evil.example.com/services/data", "//evil.example.com/x", "/etc/passwd", "../oauth2/token"],
)
def test_request_rejects_foreign_targets(
    h: Harness, mock: respx.MockRouter, rec: Recorder, path: str
) -> None:
    sf = h.client(recorder=rec)
    with pytest.raises(ValueError):
        h.run(sf.request("GET", path))
    assert not mock.calls
    assert rec.requests[-1]["outcome"] == "error"


def test_request_errors_are_typed(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/nope").respond(404, json=[{"errorCode": "NOT_FOUND", "message": "nope"}])
    sf = h.client()
    with pytest.raises(esf.NotFound):
        h.run(sf.request("GET", "nope"))


def test_instance_url_override(h: Harness, mock: respx.MockRouter) -> None:
    other = "https://override.my.salesforce.com"
    route = mock.get(f"{other}/services/data/v68.0/limits").respond(json={})
    sf = h.client(instance_url=other + "/")
    h.run(sf.limits())
    assert route.called


# --- limits -------------------------------------------------------------------------------------


def test_limits(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/limits").respond(json=load_fixture("misc/limits.json"))
    sf = h.client()
    lim = h.run(sf.limits())
    assert set(lim) == {"DailyApiRequests", "DataStorageMB"}
    daily = lim["DailyApiRequests"]
    assert (daily.max, daily.remaining, daily.used) == (100000, 88000, 12000)
    assert daily.details == {"Ant Migration Tool": {"Max": 0, "Remaining": 0}}
    snap = sf.usage.snapshot()
    assert (snap.api_used, snap.api_max) == (12000, 100000)


def test_limits_without_daily(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/limits").respond(json=[])
    sf = h.client()
    assert h.run(sf.limits()) == {}
    assert sf.usage.snapshot().api_used is None


def test_limits_poller(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/limits").respond(json=load_fixture("misc/limits.json"))
    sf = h.client()
    with pytest.raises(ValueError):
        sf.start_limits_poller(10)
    if h.is_async:

        async def go() -> None:
            import asyncio

            sf.start_limits_poller()
            sf.start_limits_poller()  # idempotent
            for _ in range(5):
                await asyncio.sleep(0)
            await sf.aclose()

        h.run(go())
    else:
        import time

        sf.start_limits_poller()
        sf.start_limits_poller()
        deadline = time.time() + 2
        while not route.called and time.time() < deadline:
            time.sleep(0.01)
        sf.close()
    assert route.call_count == 1
    assert sf._transport.poller is None


# --- describe cache ---------------------------------------------------------------------------------


class DictCache:
    """A Cache implementation the test can inspect and age."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttls: dict[str, float] = {}

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def set(self, key: str, value: Any, ttl: float) -> None:
        self.data[key] = value
        self.ttls[key] = ttl

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def age(self, seconds: float) -> None:
        for v in self.data.values():
            v["fetched_at"] -= seconds


def test_describe_ttl_hit(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{DATA}/sobjects/Lead/describe").respond(
        json=load_fixture("sobject/describe_lead.json"),
        headers={"Last-Modified": "Wed, 01 Jan 2026 00:00:00 GMT"},
    )
    cache = DictCache()
    sf = h.client(cache=cache, recorder=rec)
    d1 = h.run(sf.sobject("Lead").describe())
    d2 = h.run(sf.sobject("Lead").describe())
    assert d1 == d2
    assert d1["name"] == "Lead"
    assert route.call_count == 1
    (key,) = cache.data
    assert key.startswith("esf:describe:00Dxx0000000001EAA:68.0:sobject:Lead:")
    assert cache.ttls[key] == esf.DescribeCacheConfig().stale_ttl
    assert rec.requests[0]["operation"] == "sobject.describe"
    # cached=False always asks Salesforce
    h.run(sf.sobject("Lead").describe(cached=False))
    assert route.call_count == 2
    assert "If-Modified-Since" not in route.calls.last.request.headers


def test_describe_stale_revalidates_with_304(h: Harness, mock: respx.MockRouter) -> None:
    lm = "Wed, 01 Jan 2026 00:00:00 GMT"
    route = mock.get(f"{DATA}/sobjects/Lead/describe")
    route.side_effect = [
        httpx.Response(200, json=load_fixture("sobject/describe_lead.json"), headers={"Last-Modified": lm}),
        httpx.Response(304),
        httpx.Response(200, json={"name": "Lead", "fields": [], "v": 2}),
    ]
    cache = DictCache()
    sf = h.client(cache=cache, describe_cache=esf.DescribeCacheConfig(ttl=60))
    first = h.run(sf.sobject("Lead").describe())
    cache.age(61)
    again = h.run(sf.sobject("Lead").describe())
    assert again == first
    assert route.calls[1].request.headers["If-Modified-Since"] == lm
    # The 304 refreshed the entry: fresh again without a request.
    h.run(sf.sobject("Lead").describe())
    assert route.call_count == 2
    entry = next(iter(cache.data.values()))
    assert entry["last_modified"] == lm
    cache.age(61)
    third = h.run(sf.sobject("Lead").describe())
    assert third["v"] == 2
    assert route.calls[2].request.headers["If-Modified-Since"] == lm


def test_describe_stale_without_revalidation(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/sobjects").respond(json=load_fixture("sobject/describe_global.json"))
    cache = DictCache()
    sf = h.client(cache=cache, describe_cache=esf.DescribeCacheConfig(ttl=60, revalidate=False))
    g = h.run(sf.describe_global())
    assert g["sobjects"][0]["name"] == "Account"
    cache.age(61)
    h.run(sf.describe_global())
    assert route.call_count == 2
    assert "If-Modified-Since" not in route.calls.last.request.headers
    key = next(iter(cache.data))
    assert ":global:" in key
    # No Last-Modified header → the library records its own fetch time.
    assert cache.data[key]["last_modified"].endswith("GMT")


def test_describe_cache_keyed_by_user(h: Harness, mock: respx.MockRouter) -> None:
    from ..conftest import make_token, web_auth

    route = mock.get(f"{DATA}/sobjects/Lead/describe").respond(json={"name": "Lead"})
    cache = esf.MemoryCache()
    sf = h.client(cache=cache)
    other_store = h.new_store()
    tok = make_token("AT-user-2").replace(user_id="005xx0000000002AAA")
    auth = web_auth(token_key="user:2")
    h.seed(other_store, auth.token_key, tok)
    child = sf.with_auth(auth, store=other_store)
    h.run(sf.sobject("Lead").describe())
    h.run(child.sobject("Lead").describe())
    h.run(child.sobject("Lead").describe())
    assert route.call_count == 2  # one per user; shared cache
    assert len(cache) == 2


def test_default_memory_cache_is_used(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/sobjects/Lead/describe").respond(json={"name": "Lead"})
    sf = h.client()
    h.run(sf.sobject("Lead").describe())
    h.run(sf.sobject("Lead").describe())
    assert route.call_count == 1


@pytest.mark.parametrize("name", ["Content-Type", "content-type", "CONTENT-TYPE"])
def test_request_content_type_header_any_case(h: Harness, mock: respx.MockRouter, name: str) -> None:
    route = mock.post(f"{DATA}/x").respond(204)
    sf = h.client()
    h.run(sf.request("POST", "x", content=b"a,b\n1,2\n", headers={name: "text/csv"}))
    req = route.calls.last.request
    assert req.headers.get_list("Content-Type") == ["text/csv"]


def test_request_json_with_custom_content_type(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/x").respond(204)
    sf = h.client()
    h.run(sf.request("POST", "x", json={"a": 1}, headers={"content-type": "application/json; charset=UTF-8"}))
    assert route.calls.last.request.headers.get_list("Content-Type") == ["application/json; charset=UTF-8"]
    h.run(sf.request("POST", "x", json={"a": 1}))
    assert route.calls.last.request.headers.get_list("Content-Type") == ["application/json"]
