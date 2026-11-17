"""``TokenProvider``: a login that calls the user's function for a token (§6.1, §6.4).

The ``401`` body is the recorded ``errors/invalid_session.json``. The ``200 {}`` answers to
``/limits`` are **synthetic**: these tests check which token was sent, not the body.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, Harness, Recorder, make_token
from ..fixtures import load_fixture
from .test_refresh_concurrency import run_concurrently, slow_transport

URL = f"{DATA}/limits"
REVOKE_URL = f"{INSTANCE}/services/oauth2/revoke"
SECRET = "AT-secret-do-not-leak-0042"


def rejected() -> httpx.Response:
    return httpx.Response(401, json=load_fixture("errors/invalid_session.json"))


class Provider:
    """Returns ``AT-provided-1``, ``AT-provided-2``, ... and records the calling thread."""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.lock = threading.Lock()
        self.calls = 0
        self.threads: list[int] = []
        self.tokens = tokens

    def __call__(self) -> esf.Token:
        with self.lock:
            self.calls += 1
            n = self.calls
            self.threads.append(threading.get_ident())
        access = self.tokens[min(n, len(self.tokens)) - 1] if self.tokens else f"AT-provided-{n}"
        return esf.Token(access_token=access, instance_url=INSTANCE, issued_at=datetime.now(timezone.utc))


class Api:
    """401 for every token in ``reject``; synthetic ``200 {}`` otherwise."""

    def __init__(self, reject: set[str]) -> None:
        self.reject = reject
        self.lock = threading.Lock()
        self.sent: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        tok = request.headers["Authorization"].removeprefix("Bearer ")
        with self.lock:
            self.sent.append(tok)
        return rejected() if tok in self.reject else httpx.Response(200, json={})  # synthetic


def client(h: Harness, auth: esf.TokenProvider, rec: Recorder | None = None, **kw: Any) -> Any:
    return h.client(auth, token=False, recorder=rec, **kw)


def test_first_call_gets_the_token_from_the_provider(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    provider = Provider()
    api = Api(set())
    mock.get(URL).mock(side_effect=api)
    sf = client(h, esf.TokenProvider(provider), rec)

    assert h.run(sf.limits()) == {}
    assert h.run(sf.limits()) == {}

    assert provider.calls == 1
    assert api.sent == ["AT-provided-1", "AT-provided-1"]
    [refreshed] = rec.named("sf.auth.refreshed")
    assert refreshed["flow"] == "token_provider"
    assert refreshed["initial"] is True
    # The async client must not block the event loop: the provider runs in a worker thread.
    main = threading.get_ident()
    assert (provider.threads[0] != main) is h.is_async


def test_401_calls_the_provider_once_and_resends(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    provider = Provider()
    api = Api({"AT-provided-1"})
    mock.get(URL).mock(side_effect=api)
    sf = client(h, esf.TokenProvider(provider), rec)

    assert h.run(sf.limits()) == {}

    assert provider.calls == 2
    assert api.sent == ["AT-provided-1", "AT-provided-2"]
    events = rec.named("sf.auth.refreshed")
    assert [e["initial"] for e in events] == [True, False]
    assert h.run(sf.auth.token()).access_token == "AT-provided-2"


def test_a_second_401_raises_session_expired(h: Harness, mock: respx.MockRouter) -> None:
    provider = Provider()
    api = Api({"AT-provided-1", "AT-provided-2"})
    mock.get(URL).mock(side_effect=api)
    sf = client(h, esf.TokenProvider(provider))

    with pytest.raises(esf.SessionExpired) as info:
        h.run(sf.limits())

    assert "still rejected after one token refresh" in str(info.value)
    assert provider.calls == 2  # the first token, and one refresh
    assert api.sent == ["AT-provided-1", "AT-provided-2"]


def test_concurrent_401s_make_one_provider_call(h: Harness, mock: respx.MockRouter) -> None:
    provider = Provider()
    api = Api({"AT-provided-1"})
    mock.get(URL).mock(side_effect=api)
    sf = client(h, esf.TokenProvider(provider))
    h.run(sf.auth.token())  # the first token, before the race
    slow_transport(h, sf, 0.002)

    results = run_concurrently(h, [sf.limits for _ in range(30)])

    assert results == [{}] * 30
    assert provider.calls == 2
    assert api.sent.count("AT-provided-2") == 30


def test_concurrent_401s_share_a_refresh_that_returned_the_same_token(
    h: Harness, mock: respx.MockRouter
) -> None:
    """A provider can hand back the token Salesforce just rejected (a CLI's cache, say).

    The callers that waited on the refresh reuse its result instead of each calling again.
    """
    provider = Provider(["AT-same"])
    api = Api({"AT-same"})
    mock.get(URL).mock(side_effect=api)
    sf = client(h, esf.TokenProvider(provider))
    h.run(sf.auth.token())
    slow_transport(h, sf, 0.002)

    def call() -> Any:
        async def go() -> str:
            try:
                await sf.limits()
            except esf.SessionExpired:
                return "expired"
            return "ok"  # pragma: no cover - the API always rejects

        if h.is_async:
            return go()
        try:
            sf.limits()
        except esf.SessionExpired:
            return "expired"
        return "ok"  # pragma: no cover

    results = run_concurrently(h, [call for _ in range(20)])

    assert results == ["expired"] * 20
    assert provider.calls == 2  # the first token, and one shared refresh


def test_a_provider_that_raises_gives_refresh_failed_without_its_message(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    calls: list[int] = []

    def broken() -> esf.Token:
        calls.append(1)
        raise RuntimeError(f"sf org display failed: {SECRET}")

    mock.get(URL).mock(side_effect=Api(set()))
    sf = client(h, esf.TokenProvider(broken), rec)

    with pytest.raises(esf.RefreshFailed) as info:
        h.run(sf.limits())

    err = info.value
    assert "RuntimeError" in str(err)
    assert SECRET not in str(err)
    assert SECRET not in repr(err)
    assert isinstance(err.__cause__, RuntimeError)  # kept for debugging, not in the message
    for event in rec.events:
        assert SECRET not in repr(event.fields)
    # A later call tries again.
    with pytest.raises(esf.RefreshFailed):
        h.run(sf.limits())
    assert len(calls) == 2


def test_a_salesforce_error_from_the_provider_is_raised_unchanged(h: Harness, mock: respx.MockRouter) -> None:
    def logged_out() -> esf.Token:
        raise esf.ReauthorizationRequired("run sf org login web")

    sf = client(h, esf.TokenProvider(logged_out))

    with pytest.raises(esf.ReauthorizationRequired, match="sf org login web"):
        h.run(sf.limits())


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"access_token": SECRET, "instance_url": INSTANCE}, "returned dict, not an esf.Token"),
        (make_token(SECRET, instance_url="http://evil.example"), "token provider instance_url"),
    ],
)
def test_a_bad_provider_result_gives_refresh_failed(
    h: Harness, mock: respx.MockRouter, result: Any, message: str
) -> None:
    api = Api(set())
    mock.get(URL).mock(side_effect=api)
    sf = client(h, esf.TokenProvider(lambda: result))

    with pytest.raises(esf.RefreshFailed, match=message) as info:
        h.run(sf.limits())

    assert SECRET not in str(info.value)
    assert api.sent == []  # the token was never sent


def test_no_token_in_events_or_errors(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    provider = Provider([SECRET, SECRET + "-2"])
    mock.get(URL).mock(side_effect=Api({SECRET, SECRET + "-2"}))
    auth = esf.TokenProvider(provider)
    sf = client(h, auth, rec)

    with pytest.raises(esf.SessionExpired) as info:
        h.run(sf.limits())

    assert SECRET not in str(info.value)
    assert SECRET not in repr(info.value)
    assert rec.events
    for event in rec.events:
        assert SECRET not in repr(event.fields)
    assert "get_token" not in repr(auth)


def test_a_shared_store_with_token_key_skips_the_provider(h: Harness, mock: respx.MockRouter) -> None:
    provider = Provider()
    api = Api(set())
    mock.get(URL).mock(side_effect=api)
    store = h.new_store()
    h.seed(store, "cli:dev", make_token("AT-from-another-process"))
    sf = client(h, esf.TokenProvider(provider, token_key="cli:dev"), store=store)

    assert h.run(sf.limits()) == {}

    assert provider.calls == 0
    assert api.sent == ["AT-from-another-process"]


def test_the_provider_token_is_saved_to_the_store(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(URL).mock(side_effect=Api(set()))
    store = h.new_store()
    auth = esf.TokenProvider(Provider())
    sf = client(h, auth, store=store)

    h.run(sf.limits())

    saved = h.load(store, auth.token_key)
    assert saved is not None
    assert saved.access_token == "AT-provided-1"


def test_revoke_and_introspect_match_static_token(h: Harness, mock: respx.MockRouter) -> None:
    """Revoke goes to the token's instance; other keys need ``auth=``, as with StaticToken."""
    revoke = mock.post(REVOKE_URL).respond(200)
    store = h.new_store()
    auth = esf.TokenProvider(Provider())
    sf = client(h, auth, store=store)
    h.run(sf.auth.token())

    with pytest.raises(ValueError, match="TokenProvider, which has no OAuth client"):
        h.run(sf.auth.introspect("someone-else"))

    h.run(sf.auth.revoke())
    assert revoke.call_count == 1
    assert revoke.calls.last.request.content == b"token=AT-provided-1"
    assert h.load(store, auth.token_key) is None


def test_force_refresh_calls_the_provider(h: Harness) -> None:
    provider = Provider()
    sf = client(h, esf.TokenProvider(provider))
    old: esf.Token = h.run(sf.auth.token())

    new: esf.Token = h.run(sf.auth.refresh())

    assert (old.access_token, new.access_token) == ("AT-provided-1", "AT-provided-2")
    assert provider.calls == 2


def test_expires_at_from_the_provider_triggers_a_proactive_refresh(
    h: Harness, mock: respx.MockRouter
) -> None:
    issued = datetime.now(timezone.utc)
    tokens: list[esf.Token] = [
        esf.Token(access_token="AT-old", instance_url=INSTANCE, issued_at=issued, expires_at=issued),
        esf.Token(access_token="AT-new", instance_url=INSTANCE, issued_at=issued),
    ]
    get: Callable[[], esf.Token] = lambda: tokens.pop(0)  # noqa: E731
    api = Api(set())
    mock.get(URL).mock(side_effect=api)
    sf = client(h, esf.TokenProvider(get))

    h.run(sf.limits())

    assert api.sent == ["AT-new"]
