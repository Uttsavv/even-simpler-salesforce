"""Performance benchmarks (§12). Skipped unless run with ``--benchmark-enable`` or ``-m bench``.

All traffic goes through ``httpx.MockTransport`` with synthetic recorded bodies, so the numbers
measure the library, not the network.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.core import jsonlib
from even_simpler_salesforce.core.http import RawResponse

from ..conftest import DATA, INSTANCE, TOKEN_URL, make_token, token_body, web_auth

pytestmark = pytest.mark.bench

ROWS_PER_PAGE = 2000


def page(n: int, *, done: bool, nxt: str | None = None) -> bytes:
    body: dict[str, Any] = {
        "totalSize": n * 10,
        "done": done,
        "records": [
            {
                "attributes": {"type": "Lead", "url": f"/services/data/v68.0/sobjects/Lead/00Qxx{i:013d}"},
                "Id": f"00Qxx{i:013d}",
                "Name": f"Lead {i}",
                "Email": f"lead{i}@example.com",
                "Status": "Open",
                "AnnualRevenue": i * 10.5,
            }
            for i in range(n)
        ],
    }
    if nxt:
        body["nextRecordsUrl"] = nxt
    return json.dumps(body).encode()


PAGE_2000 = page(ROWS_PER_PAGE, done=True)


def mock_handler(
    routes: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> Callable[..., httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes[request.url.path](request)

    return handler


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


def static_client(kind: str, handler: Callable[..., httpx.Response]) -> Any:
    auth = esf.StaticToken("00D!bench", INSTANCE)
    if kind == "async":
        return esf.AsyncSalesforce(
            auth, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
    return esf.Salesforce(auth, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


# --- client overhead per call (target < 0.3 ms over raw httpx) -------------------------------------


def _limits_handler() -> Callable[..., httpx.Response]:
    return mock_handler({"/services/data/v68.0/limits": lambda r: httpx.Response(200, json={})})


def test_raw_httpx_call(benchmark: Any) -> None:
    http = httpx.Client(transport=httpx.MockTransport(_limits_handler()))
    benchmark(lambda: http.get(f"{DATA}/limits", headers={"Authorization": "Bearer x"}).json())


def test_client_call_sync(benchmark: Any) -> None:
    sf = static_client("sync", _limits_handler())
    benchmark(sf.limits)


def test_client_call_async(benchmark: Any, loop: asyncio.AbstractEventLoop) -> None:
    sf = static_client("async", _limits_handler())
    benchmark(lambda: loop.run_until_complete(sf.limits()))


# --- JSON parse of a 2,000-row page ----------------------------------------------------------------------


def test_json_parse_2000_rows(benchmark: Any) -> None:
    def parse() -> int:
        return len(RawResponse(200, {"content-type": "application/json"}, PAGE_2000).json()["records"])

    assert benchmark(parse) == ROWS_PER_PAGE
    benchmark.extra_info["json_backend"] = jsonlib.BACKEND


def test_query_page_2000_rows(benchmark: Any) -> None:
    sf = static_client(
        "sync", mock_handler({"/services/data/v68.0/query": lambda r: httpx.Response(200, content=PAGE_2000)})
    )
    assert benchmark(lambda: len(sf.query("SELECT Id FROM Lead").records)) == ROWS_PER_PAGE


# --- query_iter over 10 pages -----------------------------------------------------------------------------


def _paged_handler(pages: int) -> Callable[..., httpx.Response]:
    bodies = {}
    for i in range(pages):
        nxt = f"/services/data/v68.0/query/01gxx0000000001AAA-{(i + 1) * 2000}" if i < pages - 1 else None
        bodies[i] = page(ROWS_PER_PAGE, done=nxt is None, nxt=nxt)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        idx = 0 if path.endswith("/query") else int(path.rsplit("-", 1)[1]) // 2000
        return httpx.Response(200, content=bodies[idx])

    return handler


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_query_iter_10_pages(benchmark: Any, loop: asyncio.AbstractEventLoop, kind: str) -> None:
    sf = static_client(kind, _paged_handler(10))

    if kind == "sync":

        def run() -> int:
            return sum(1 for _ in sf.query_iter("SELECT Id FROM Lead"))

    else:

        async def consume() -> int:
            return len([r async for r in sf.query_iter("SELECT Id FROM Lead")])

        def run() -> int:
            return loop.run_until_complete(consume())

    assert benchmark(run) == 10 * ROWS_PER_PAGE


# --- Collections chunking of 10,000 rows ---------------------------------------------------------------


def _echo_collections(request: httpx.Request) -> httpx.Response:
    import gzip

    raw = request.content
    if request.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    n = len(json.loads(raw)["records"])
    return httpx.Response(200, json=[{"id": "001xx0000000001AAA", "success": True, "errors": []}] * n)


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_collections_10000_rows(benchmark: Any, loop: asyncio.AbstractEventLoop, kind: str) -> None:
    sf = static_client(kind, mock_handler({"/services/data/v68.0/composite/sobjects": _echo_collections}))
    rows = [{"Name": f"Account {i}", "Description": "x" * 50} for i in range(10_000)]

    def run() -> int:
        res = sf.collections.create(rows, sobject="Account")
        if kind == "async":
            res = loop.run_until_complete(res)
        return len(res)

    assert benchmark(run) == 10_000


# --- Bulk CSV parse of 100k rows ----------------------------------------------------------------------


def test_bulk_csv_parse_100k_rows(benchmark: Any) -> None:
    pytest.skip("Bulk API 2.0 (sf.bulk) ships in 0.3 (§19); benchmark lands with it")


# --- refresh single-flight with 100 concurrent callers -------------------------------------------------


def _refresh_handler(counter: dict[str, int]) -> Callable[..., httpx.Response]:
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            with lock:
                counter["token_posts"] += 1
            return httpx.Response(200, json=token_body("AT-fresh", "RT-fresh"))
        if request.headers["Authorization"] == "Bearer AT-stale":
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "expired"}])
        return httpx.Response(200, json={})

    return handler


def test_refresh_single_flight_100_callers(benchmark: Any, loop: asyncio.AbstractEventLoop) -> None:
    counter = {"token_posts": 0}
    handler = _refresh_handler(counter)
    auth = web_auth()

    def run() -> int:
        counter["token_posts"] = 0
        store = esf.AsyncInMemoryTokenStore()
        loop.run_until_complete(
            store.save(auth.token_key, make_token("AT-stale", "RT-1"), expected_version=None)
        )
        sf = esf.AsyncSalesforce(
            auth, store=store, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

        async def all_() -> None:
            await asyncio.gather(*(sf.limits() for _ in range(100)))

        loop.run_until_complete(all_())
        return counter["token_posts"]

    assert benchmark(run) == 1
    assert TOKEN_URL.endswith("/services/oauth2/token")
