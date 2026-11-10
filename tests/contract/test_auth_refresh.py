"""``sf.auth.refresh()``: a forced refresh that shares the single-flight refresh (§6.4)."""

from __future__ import annotations

import asyncio
import threading
from urllib.parse import parse_qs

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, TOKEN_URL, Harness, Recorder, make_token, token_body, web_auth
from .test_refresh_concurrency import run_concurrently, slow_transport

URL = f"{DATA}/limits"


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


class Endpoint:
    """Token endpoint that numbers the tokens it issues."""

    def __init__(self, refresh: str | None = None) -> None:
        self.lock = threading.Lock()
        self.count = 0
        self.refresh = refresh

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.count += 1
            n = self.count
        return httpx.Response(200, json=token_body(f"AT-forced-{n}", self.refresh))


def test_refresh_replaces_a_valid_token(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    endpoint = Endpoint()
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    api = mock.get(URL).respond(json={})
    store = h.new_store()
    sf = h.client(store=store, token=make_token("AT-still-valid"), recorder=rec)

    new = h.run(sf.auth.refresh())

    assert isinstance(new, esf.Token)
    assert new.access_token == "AT-forced-1"
    assert endpoint.count == 1
    assert h.load(store, sf._runtime.key).access_token == "AT-forced-1"
    assert h.run(sf.auth.token()).access_token == "AT-forced-1"
    h.run(sf.limits())
    assert api.calls.last.request.headers["Authorization"] == "Bearer AT-forced-1"
    (ev,) = rec.named("sf.auth.refreshed")
    assert (ev["flow"], ev["proactive"], ev["initial"]) == ("client_credentials", False, False)
    assert sf.usage.snapshot().refreshes == 1


def test_each_call_refreshes_again(h: Harness, mock: respx.MockRouter) -> None:
    endpoint = Endpoint()
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    sf = h.client()
    assert h.run(sf.auth.refresh()).access_token == "AT-forced-1"
    assert h.run(sf.auth.refresh()).access_token == "AT-forced-2"
    assert endpoint.count == 2


def test_concurrent_calls_share_one_refresh(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    endpoint = Endpoint()
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    sf = h.client(recorder=rec)
    slow_transport(h, sf, 0.01)

    results = run_concurrently(h, [sf.auth.refresh for _ in range(20)])

    assert endpoint.count == 1
    assert {t.access_token for t in results} == {"AT-forced-1"}
    assert len(rec.named("sf.auth.refreshed")) == 1


def test_concurrent_calls_share_one_failure(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(TOKEN_URL).respond(
        400, json={"error": "invalid_client", "error_description": "invalid client credentials"}
    )
    sf = h.client()
    slow_transport(h, sf, 0.01)

    if h.is_async:

        async def all_() -> list[object]:
            calls = (sf.auth.refresh() for _ in range(10))
            return list(await asyncio.gather(*calls, return_exceptions=True))

        results = h.run(all_())
    else:

        def call() -> object:
            try:
                return sf.auth.refresh()
            except esf.OAuthError as exc:
                return exc

        results = run_concurrently(h, [call for _ in range(10)])

    assert route.call_count == 1
    assert all(isinstance(r, esf.OAuthError) and r.error == "invalid_client" for r in results)


def test_web_server_flow_uses_and_rotates_the_refresh_token(h: Harness, mock: respx.MockRouter) -> None:
    tok = mock.post(TOKEN_URL).mock(side_effect=Endpoint(refresh="RT-rotated"))
    store = h.new_store()
    auth = web_auth()
    sf = h.client(auth, store=store, token=make_token("AT-old", "RT-original"))

    new = h.run(sf.auth.refresh())

    f = form(tok.calls.last.request)
    assert (f["grant_type"], f["refresh_token"]) == ("refresh_token", "RT-original")
    assert (new.access_token, new.refresh_token) == ("AT-forced-1", "RT-rotated")
    saved = h.load(store, auth.token_key)
    assert (saved.access_token, saved.refresh_token) == ("AT-forced-1", "RT-rotated")


def test_refresh_uses_the_stored_token_when_memory_is_empty(h: Harness, mock: respx.MockRouter) -> None:
    """A new process with a stored token: the stored token is replaced, not handed back."""
    tok = mock.post(TOKEN_URL).mock(side_effect=Endpoint(refresh="RT-2"))
    sf = h.client(web_auth(), token=make_token("AT-stored", "RT-1"))
    assert sf._runtime.current() is None
    assert h.run(sf.auth.refresh()).access_token == "AT-forced-1"
    assert form(tok.calls.last.request)["refresh_token"] == "RT-1"


def test_refresh_without_any_token_acquires_one(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.post(TOKEN_URL).mock(side_effect=Endpoint())
    sf = h.client(token=False, recorder=rec)
    assert h.run(sf.auth.refresh()).access_token == "AT-forced-1"
    (ev,) = rec.named("sf.auth.refreshed")
    assert ev["initial"] is True


def test_web_server_flow_without_refresh_token(h: Harness, mock: respx.MockRouter) -> None:
    tok = mock.post(TOKEN_URL)
    sf = h.client(web_auth(), token=False)
    with pytest.raises(esf.ReauthorizationRequired):
        h.run(sf.auth.refresh())
    assert not tok.called


def test_static_token_raises_session_expired(h: Harness, mock: respx.MockRouter) -> None:
    tok = mock.post(TOKEN_URL)
    sf = h.client(esf.StaticToken("00D-static-token", INSTANCE))
    with pytest.raises(esf.SessionExpired) as ei:
        h.run(sf.auth.refresh())
    assert ei.value.operation == "oauth.refresh"
    assert "StaticToken" in ei.value.message
    assert not tok.called
