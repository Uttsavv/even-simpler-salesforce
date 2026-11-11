"""Client family: with_auth, tagged, call_context, budget, capture, lifecycle (§5.2, §10, §11.5)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, Harness, Recorder, make_token, web_auth

LIMITS = f"{DATA}/limits"


def limits_with_usage(used: int, mx: int = 100) -> httpx.Response:
    return httpx.Response(200, json={}, headers={"Sforce-Limit-Info": f"api-usage={used}/{mx}"})


# --- with_auth -------------------------------------------------------------------------------


def test_with_auth_child_shares_transport_but_not_token(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    route = mock.get(LIMITS).respond(json={})
    store = h.new_store()
    base = h.client(store=store, recorder=rec)
    user_auth = web_auth(token_key="user:1")
    h.seed(store, "user:1", make_token("AT-user-1", "RT-user-1"))
    child = base.with_auth(user_auth)
    assert child._transport is base._transport
    assert child.usage._s is base.usage._s  # pyright: ignore[reportPrivateUsage]
    assert child.observers is base.observers
    assert child.api_version == base.api_version == "68.0"
    h.run(base.limits())
    h.run(child.limits())
    auths = [c.request.headers["Authorization"] for c in route.calls]
    assert auths == ["Bearer AT-initial-0001", "Bearer AT-user-1"]
    assert len(rec.requests) == 2  # shared observers
    assert child.usage.snapshot().calls_by_operation == {"limits": 2}  # shared counters
    assert base.usage.snapshot().calls_by_operation == {"limits": 2}
    assert base.usage.snapshot().calls_by_operation == {"limits": 2}
    # Children don't close the pool.
    h.run(child.aclose() if h.is_async else child.close())
    assert not base._transport.closed
    h.run(base.limits())


def test_with_auth_own_store(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(LIMITS).respond(json={})
    base = h.client()
    other = h.new_store()
    h.seed(other, "user:2", make_token("AT-other-store"))
    child = base.with_auth(web_auth(token_key="user:2"), store=other)
    h.run(child.limits())
    assert route.calls.last.request.headers["Authorization"] == "Bearer AT-other-store"


def test_children_with_same_key_share_memory_token(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(LIMITS).respond(json={})
    store = h.new_store()
    base = h.client(store=store)
    h.seed(store, "user:3", make_token("AT-3", "RT-3"))
    a = base.with_auth(web_auth(token_key="user:3"))
    b = base.with_auth(web_auth(token_key="user:3"))
    h.run(a.limits())
    assert b._runtime.current() is a._runtime.current()


# --- tagged / call_context -----------------------------------------------------------------------------


def test_tagged_and_call_context(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(LIMITS).respond(json={})
    sf = h.client(recorder=rec)
    job = sf.tagged(caller="job:backfill", priority="low")
    h.run(job.limits())
    with esf.call_context(caller="route:/leads", priority="high"):
        h.run(sf.limits())
        h.run(job.limits())  # the child's own tags win
    h.run(job.tagged(priority="high").limits())  # inherits caller
    got = [(e["caller"], e["priority"]) for e in rec.requests]
    assert got == [
        ("job:backfill", "low"),
        ("route:/leads", "high"),
        ("job:backfill", "low"),
        ("job:backfill", "high"),
    ]
    assert sf.usage.snapshot().calls_by_caller == {"job:backfill": 3, "route:/leads": 1}


def test_call_context_in_concurrent_chunks(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        import json

        n = len(json.loads(request.content)["records"])
        return httpx.Response(200, json=[{"id": "x", "success": True}] * n)

    mock.post(f"{DATA}/composite/sobjects").mock(side_effect=echo)
    sf = h.client(recorder=rec)
    with esf.call_context(caller="ctx-caller"), esf.capture() as summary:
        h.run(sf.collections.create([{"Name": str(i)} for i in range(450)], sobject="Account"))
    assert {e["caller"] for e in rec.requests} == {"ctx-caller"}
    assert summary.as_fields()["sf_calls"] == 3


# --- budget guard ---------------------------------------------------------------------------------------


def test_budget_guard(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(LIMITS).mock(return_value=limits_with_usage(85))
    sf = h.client(recorder=rec, budget=esf.Budget(pause_low_priority_above=0.8, block_all_above=0.9))
    h.run(sf.limits())  # learn usage from the header
    low = sf.tagged(priority="low")
    with pytest.raises(esf.BudgetExceeded) as ei:
        h.run(low.limits())
    assert ei.value.usage.api_used == 85
    assert route.call_count == 1  # never sent
    assert rec.requests[-1]["error_class"] == "BudgetExceeded"
    assert rec.requests[-1]["attempts"] == 0
    h.run(sf.limits())  # normal still allowed at 85 %
    route.mock(return_value=limits_with_usage(95))
    h.run(sf.limits())
    with pytest.raises(esf.BudgetExceeded):
        h.run(sf.limits())  # normal blocked above 90 %
    with esf.call_context(priority="high"):
        h.run(sf.limits())  # high always allowed
    assert sf.usage.snapshot().errors_by_code["BUDGET_EXCEEDED"] == 2


def test_no_budget_never_blocks(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(LIMITS).mock(return_value=limits_with_usage(100))
    sf = h.client()
    for _ in range(3):
        h.run(sf.tagged(priority="low").limits())


# --- error_tags / config validation ------------------------------------------------------------------------


def test_error_tags_validation(h: Harness) -> None:
    with pytest.raises(TypeError, match="error_tags"):
        h.Client(esf.StaticToken("x", INSTANCE), error_tags={"X": ValueError})
    with pytest.raises(TypeError, match="error_tags"):
        h.Client(esf.StaticToken("x", INSTANCE), error_tags={"X": "nope"})


@pytest.mark.parametrize("version", ["68", "v68.0", "58.0", "1.0", "68.00"])
def test_api_version_validation(h: Harness, version: str) -> None:
    with pytest.raises(ValueError, match="api_version"):
        h.Client(esf.StaticToken("x", INSTANCE), api_version=version)


def test_api_version_messages_come_from_the_constants(h: Harness) -> None:
    from even_simpler_salesforce._async.client import DEFAULT_API_VERSION, MIN_API_VERSION

    with pytest.raises(ValueError, match=f"must look like '{DEFAULT_API_VERSION}'"):
        h.Client(esf.StaticToken("x", INSTANCE), api_version="v1")
    minimum = ".".join(map(str, MIN_API_VERSION))
    with pytest.raises(ValueError, match=f"minimum supported v{minimum}$"):
        h.Client(esf.StaticToken("x", INSTANCE), api_version="58.0")


def test_custom_api_version_in_urls(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{INSTANCE}/services/data/v62.0/limits").respond(json={})
    sf = h.client(api_version="62.0", recorder=rec)
    h.run(sf.limits())
    assert route.called
    assert rec.requests[0]["api_version"] == "62.0"


def test_custom_json_codec_and_user_agent(h: Harness, mock: respx.MockRouter) -> None:
    import json

    class Codec:
        def __init__(self) -> None:
            self.loads_calls = 0

        def dumps(self, obj: Any) -> bytes:
            return json.dumps({"wrapped": obj}).encode()

        def loads(self, data: bytes | str) -> Any:
            self.loads_calls += 1
            return json.loads(data)

    codec = Codec()
    route = mock.post(f"{DATA}/sobjects/Lead/").respond(201, json={"id": "00Qxx0000000001AAA"})
    sf = h.client(json=codec, user_agent="my-app/1.0")
    h.run(sf.sobject("Lead").create({"a": 1}))
    req = route.calls.last.request
    assert json.loads(req.content) == {"wrapped": {"a": 1}}
    assert req.headers["User-Agent"] == "my-app/1.0"
    assert codec.loads_calls >= 1


# --- capture across children and awaits ---------------------------------------------------------------------


def test_capture_across_child_clients(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(LIMITS)
    route.side_effect = [
        httpx.Response(200, json={}, headers={"Sforce-Limit-Info": "api-usage=10/100"}),
        httpx.Response(400, json=[{"errorCode": "INVALID_FIELD", "message": "x"}]),
        httpx.Response(503),
        httpx.Response(200, json={}),
    ]
    store = h.new_store()
    sf = h.client(store=store)
    h.seed(store, "user:4", make_token("AT-4", "RT-4"))
    child = sf.with_auth(web_auth(token_key="user:4")).tagged(caller="c")
    with esf.capture() as outer:
        h.run(sf.limits())
        with esf.capture() as inner:
            with pytest.raises(esf.InvalidField):
                h.run(child.limits())
            h.run(child.limits())  # 503 then 200
    o, i = outer.as_fields(), inner.as_fields()
    assert o["sf_calls"] == 3
    assert o["sf_errors"] == ["INVALID_FIELD"]
    assert o["sf_retries"] == 1
    assert o["sf_api_used"] == 10
    assert o["sf_ops"] == {"limits": 3}
    assert i["sf_calls"] == 2


# --- concurrency gate ------------------------------------------------------------------------------------


def test_max_concurrency(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(LIMITS).respond(json={})
    sf = h.client(max_concurrency=2)
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()
    real_send = sf._transport.http.send

    if h.is_async:

        async def tracking_send(request: httpx.Request, **kw: Any) -> httpx.Response:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.01)
            try:
                return await real_send(request, **kw)
            finally:
                state["now"] -= 1

        sf._transport.http.send = tracking_send

        async def many() -> None:
            await asyncio.gather(*(sf.limits() for _ in range(8)))

        h.run(many())
    else:

        def tracking_send_sync(request: httpx.Request, **kw: Any) -> httpx.Response:
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.01)
            try:
                return real_send(request, **kw)
            finally:
                with lock:
                    state["now"] -= 1

        sf._transport.http.send = tracking_send_sync
        threads = [threading.Thread(target=sf.limits) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert state["peak"] == 2


# --- lifecycle --------------------------------------------------------------------------------------------


def test_owned_http_client_closed(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(LIMITS).respond(json={})
    sf = h.client()
    assert sf._transport.owns_http is True
    if h.is_async:

        async def use() -> None:
            async with sf as s:
                await s.limits()

        h.run(use())
    else:
        with sf as s:
            s.limits()
    assert sf._transport.http.is_closed
    h.run(sf.aclose() if h.is_async else sf.close())  # idempotent


def test_injected_http_client_not_closed(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(LIMITS).respond(json={})
    http = httpx.AsyncClient() if h.is_async else httpx.Client()
    sf = h.client(http_client=http)
    assert sf._transport.owns_http is False
    h.run(sf.limits())
    h.run(sf.aclose() if h.is_async else sf.close())
    assert not http.is_closed
    h.run(http.aclose() if h.is_async else http.close())


def test_owned_client_defaults(h: Harness) -> None:
    sf = h.client(timeout=esf.Timeouts(connect=1, read=2, write=3, pool=4))
    http = sf._transport.http
    assert http.timeout == httpx.Timeout(connect=1, read=2, write=3, pool=4)
    assert "gzip" in http.headers["Accept-Encoding"]


def test_usage_and_budget_are_per_org(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    # A with_auth() child in another org must not overwrite this org's usage or trip its guard.
    other = "https://other.my.salesforce.com"
    mock.get(LIMITS).mock(return_value=limits_with_usage(10))
    other_route = mock.get(f"{other}/services/data/v68.0/limits").mock(return_value=limits_with_usage(95))
    store = h.new_store()
    base = h.client(store=store, recorder=rec, budget=esf.Budget(pause_low_priority_above=0.8))
    h.seed(store, "org:b", make_token("AT-org-b", instance_url=other).replace(org_id="00Dxx0000000002EAA"))
    child = base.with_auth(web_auth(token_key="org:b"))
    h.run(base.limits())
    h.run(child.limits())
    assert base.usage.snapshot().api_used == 10
    assert child.usage.snapshot().api_used == 95
    assert rec.requests[-1]["api_used"] == 95
    assert base.usage.snapshot().calls_by_operation == {"limits": 2}  # counters stay family-wide
    h.run(base.tagged(priority="low").limits())  # org A at 10 %: allowed
    assert rec.requests[-1]["api_used"] == 10
    with pytest.raises(esf.BudgetExceeded) as ei:
        h.run(child.tagged(priority="low").limits())  # org B at 95 %: blocked
    assert ei.value.usage.api_used == 95
    assert other_route.call_count == 1
