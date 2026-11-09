"""Refresh single-flight under load (§6.4, §15): exactly one token POST, rotation saved first."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from ..conftest import DATA, TOKEN_URL, Harness, Recorder, make_token, token_body, web_auth

N = 100
URL = f"{DATA}/limits"
STALE = "AT-stale"


def slow_transport(h: Harness, sf: Any, delay: float) -> None:
    """Make every send yield/sleep so callers genuinely overlap (also covers the token POST)."""
    real = sf._transport.http.send
    if h.is_async:

        async def send(request: httpx.Request, **kw: Any) -> httpx.Response:
            await asyncio.sleep(delay)
            return await real(request, **kw)

    else:

        def send(request: httpx.Request, **kw: Any) -> httpx.Response:  # type: ignore[misc]
            time.sleep(delay)
            return real(request, **kw)

    sf._transport.http.send = send


def run_concurrently(h: Harness, calls: list[Callable[[], Any]]) -> list[Any]:
    if h.is_async:

        async def all_() -> list[Any]:
            return list(await asyncio.gather(*(c() for c in calls)))

        return h.run(all_())
    results: list[Any] = [None] * len(calls)
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(calls))

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = calls[i]()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    return results


class Api:
    """Thread-safe fake API: 401 for the stale token; checks the store before accepting a new one."""

    def __init__(self, store: Any, key: str) -> None:
        self.store = store
        self.key = key
        self.lock = threading.Lock()
        self.by_token: dict[str, int] = {}
        self.violations: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        tok = request.headers["Authorization"].removeprefix("Bearer ")
        with self.lock:
            self.by_token[tok] = self.by_token.get(tok, 0) + 1
        if tok == STALE:
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "expired"}])
        stored = self.store._data.get(self.key)
        if stored is None or stored.access_token != tok or stored.refresh_token != "RT-rotated":
            self.violations.append(f"{tok} used before the rotated token was saved ({stored!r})")
        return httpx.Response(200, json={})


class TokenEndpoint:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.count += 1
            n = self.count
        return httpx.Response(200, json=token_body(f"AT-fresh-{n}", "RT-rotated"))


def test_exactly_one_token_post_for_100_callers(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    store = h.new_store()
    auth = web_auth()
    api = Api(store, auth.token_key)
    endpoint = TokenEndpoint()
    mock.get(URL).mock(side_effect=api)
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    sf = h.client(auth, store=store, token=make_token(STALE, "RT-original"), recorder=rec)
    slow_transport(h, sf, 0.002)

    results = run_concurrently(h, [sf.limits for _ in range(N)])

    assert results == [{}] * N
    assert endpoint.count == 1
    assert api.violations == []
    assert api.by_token.get("AT-fresh-1") == N
    assert api.by_token.get(STALE, 0) >= 10  # many callers really raced on the expired token
    saved = h.load(store, auth.token_key)
    assert (saved.access_token, saved.refresh_token) == ("AT-fresh-1", "RT-rotated")
    assert len(rec.named("sf.auth.refreshed")) == 1
    assert sf.usage.snapshot().refreshes == 1
    assert len(rec.requests) == N
    assert all(e["outcome"] == "ok" for e in rec.requests)


def test_initial_acquisition_single_flight(h: Harness, mock: respx.MockRouter) -> None:
    endpoint = TokenEndpoint()
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    mock.get(URL).respond(json={})
    sf = h.client(token=False)  # client credentials, nothing stored yet
    slow_transport(h, sf, 0.002)
    run_concurrently(h, [sf.limits for _ in range(N)])
    assert endpoint.count == 1


@pytest.mark.parametrize("families", [2, 3])
def test_cas_conflict_between_processes(h: Harness, mock: respx.MockRouter, families: int) -> None:
    """Separate client families (≈ processes) share one store: losers adopt the winner's token."""
    store = h.new_store()
    auth = web_auth()
    h.seed(store, auth.token_key, make_token(STALE, "RT-original"))
    endpoint = TokenEndpoint()
    lock = threading.Lock()
    used: dict[str, int] = {}

    def api(request: httpx.Request) -> httpx.Response:
        tok = request.headers["Authorization"].removeprefix("Bearer ")
        with lock:
            used[tok] = used.get(tok, 0) + 1
        if tok == STALE:
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "expired"}])
        return httpx.Response(200, json={})

    mock.get(URL).mock(side_effect=api)
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    clients = [h.client(auth, store=store, token=False) for _ in range(families)]
    for sf in clients:
        slow_transport(h, sf, 0.003)

    calls = [sf.limits for sf in clients for _ in range(20)]
    results = run_concurrently(h, calls)

    assert results == [{}] * len(calls)
    assert 1 <= endpoint.count <= families  # at most one refresh per process
    final = h.load(store, auth.token_key)
    for sf in clients:
        assert sf._runtime.current().access_token == final.access_token
    # Every non-stale call used a token that was saved in the store at some point.
    assert set(used) - {STALE} <= {f"AT-fresh-{i}" for i in range(1, endpoint.count + 1)}
