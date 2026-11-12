"""A refreshed token revoked by another process's refresh (refresh-token rotation), both clients.

``synthetic``: the org model here is hand-written from what a real Developer Edition org did on
2026-11-12 (see ``big-bang.md`` §6.4): with refresh-token rotation on, **every refresh revokes
all earlier access tokens of the grant**. The official article on rotation
(https://help.salesforce.com/s/articleView?id=005316711&language=en_US&type=1) only says the
used refresh token is invalidated; the access-token revocation is org-observed.

So a call that refreshed once can still get a 401: another process refreshed again before
the call used its new token. The client then adopts the newer token from the store (at most
``MAX_ADOPTS`` times, never a second token POST of its own) and resends.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, TOKEN_URL, Harness, Recorder, make_token, token_body, web_auth

URL = f"{DATA}/limits"
APEX_URL = f"{DATA.rsplit('/data/', 1)[0]}/apexrest/esf/thing"
EXPIRED = [{"errorCode": "INVALID_SESSION_ID", "message": "Session expired or invalid"}]
LIMITS = {"DailyApiRequests": {"Max": 15000, "Remaining": 14000}}


class RotatingOrg:
    """Valid access tokens of one grant; every refresh (ours or a rival's) revokes the earlier ones."""

    def __init__(self, store: Any, key: str, *, rival_after: int = 1, rivals: int = 1) -> None:
        self.store = store
        self.key = key
        self.valid = {"AT-1"}
        self.n = 1
        self.posts = 0
        self.rival_after = rival_after  # a rival refreshes when a token is used for the first time
        self.rivals = rivals  # ... this many times in total
        self.seen: list[str] = []

    def _issue(self) -> tuple[str, str]:
        self.n += 1
        at, rt = f"AT-{self.n}", f"RT-{self.n}"
        self.valid = {at}  # rotation: earlier access tokens are revoked
        return at, rt

    def rival_refresh(self) -> None:
        """Another process refreshes and saves its token to the shared store."""
        at, rt = self._issue()
        data: dict[str, esf.Token] = self.store._data  # pyright: ignore[reportPrivateUsage]
        cur = data[self.key]
        data[self.key] = cur.replace(access_token=at, refresh_token=rt, version=cur.version + 1)

    def token(self, request: httpx.Request) -> httpx.Response:
        self.posts += 1
        at, rt = self._issue()
        return httpx.Response(200, json=token_body(at, rt))

    def api(self, request: httpx.Request) -> httpx.Response:
        at = request.headers["Authorization"].removeprefix("Bearer ")
        self.seen.append(at)
        if at != "AT-1" and self.rivals and self.seen.count(at) == 1 and len(self.seen) > self.rival_after:
            self.rivals -= 1
            self.rival_refresh()
        if at not in self.valid:
            return httpx.Response(401, json=EXPIRED)
        return httpx.Response(200, json=LIMITS)


def setup(h: Harness, mock: respx.MockRouter, rec: Recorder, **kw: Any) -> tuple[Any, RotatingOrg, Any]:
    store = h.new_store()
    auth = web_auth()
    org = RotatingOrg(store, auth.token_key, **kw)
    org.valid = set()  # the seeded AT-1 has expired: the first call refreshes
    mock.post(TOKEN_URL).mock(side_effect=org.token)
    mock.get(URL).mock(side_effect=org.api)
    sf = h.client(auth, store=store, token=make_token("AT-1", "RT-1"), recorder=rec)
    return sf, org, store


def test_superseded_token_is_replaced_by_the_newer_stored_one(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    sf, org, store = setup(h, mock, rec)
    assert h.run(sf.limits())["DailyApiRequests"].max == 15000
    # AT-1 expired -> our refresh gave AT-2 -> a rival refreshed to AT-3 (revoking AT-2) -> adopt.
    assert org.seen == ["AT-1", "AT-2", "AT-3"]
    assert org.posts == 1  # no second token POST of our own
    ev = rec.requests[-1]
    assert ev["outcome"] == "ok"
    assert ev["refreshed"] is True
    assert ev["retry_reasons"] == ["token_superseded"]
    assert [e["reason"] for e in rec.named("sf.retry")] == ["token_superseded"]
    # The adopted token is the one in memory now: the next call uses it without a refresh.
    h.run(sf.limits())
    assert org.seen[-1] == "AT-3"
    assert org.posts == 1
    stored = h.load(store, sf._runtime.key)  # pyright: ignore[reportPrivateUsage]
    assert stored is not None
    assert stored.refresh_token == "RT-3"


def test_gives_up_with_a_clear_error_when_the_store_keeps_moving(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    sf, org, _ = setup(h, mock, rec, rivals=10)
    with pytest.raises(esf.SessionExpired) as ei:
        h.run(sf.limits())
    err = ei.value
    assert "superseded by another refresh" in err.message
    assert err.hint is not None
    assert "superseded" in err.hint
    assert "'api' OAuth scope" not in err.hint
    assert org.posts == 1
    assert org.seen == ["AT-1", "AT-2", "AT-3", "AT-4"]  # one refresh, then two adopts
    assert rec.requests[-1]["retry_reasons"] == ["token_superseded", "token_superseded"]


def test_adopted_token_rejected_without_a_newer_one_keeps_the_policy_hint(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    sf, org, _ = setup(h, mock, rec)

    def adopted_also_rejected(request: httpx.Request) -> httpx.Response:
        response = org.api(request)
        if request.headers["Authorization"].endswith("AT-3"):
            return httpx.Response(401, json=EXPIRED)  # e.g. an IP or session policy
        return response

    mock.get(URL).mock(side_effect=adopted_also_rejected)
    with pytest.raises(esf.SessionExpired) as ei:
        h.run(sf.limits())
    assert "still rejected after one token refresh and 1 newer stored token(s)" in ei.value.message
    assert "'api' OAuth scope" in (ei.value.hint or "")
    assert org.seen == ["AT-1", "AT-2", "AT-3"]
    assert org.posts == 1


def test_no_newer_token_keeps_the_one_refresh_rule(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    sf, org, _ = setup(h, mock, rec, rivals=0)
    org.valid = set()
    mock.post(TOKEN_URL).respond(json=token_body("AT-x", "RT-x"))
    with pytest.raises(esf.SessionExpired) as ei:
        h.run(sf.limits())
    assert ei.value.message.endswith("(still rejected after one token refresh)")
    assert org.seen == ["AT-1", "AT-x"]
    assert rec.requests[-1]["retry_reasons"] == []


def test_a_401_that_may_come_from_code_is_not_resent_to_adopt(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    """ADR 0001: after the refresh, a POST rejected with a non-token 401 is never resent."""
    store = h.new_store()
    auth = web_auth()
    org = RotatingOrg(store, auth.token_key, rivals=0)
    org.valid = set()
    mock.post(TOKEN_URL).mock(side_effect=org.token)
    calls: list[str] = []

    def apex(request: httpx.Request) -> httpx.Response:
        at = request.headers["Authorization"].removeprefix("Bearer ")
        calls.append(at)
        if at == "AT-1":
            return httpx.Response(401, json=EXPIRED)
        org.rival_refresh()  # the store moves on, but this 401 came from the Apex class
        return httpx.Response(401, json=[{"errorCode": "APEX_ERROR", "message": "custom 401"}])

    mock.post(APEX_URL).mock(side_effect=apex)
    sf = h.client(auth, store=store, token=make_token("AT-1", "RT-1"), recorder=rec)
    with pytest.raises(esf.SalesforceError):
        h.run(sf.apex.post("/esf/thing", json={"a": 1}))
    assert calls == ["AT-1", "AT-2"]
    assert org.posts == 1


def locking_store(h: Harness) -> Any:
    """An in-memory store that records whether its lock is held."""
    base = h.new_store()
    held: list[bool] = [False]
    inner = base.lock

    if h.is_async:

        @contextlib.asynccontextmanager
        async def alock(key: str) -> AsyncIterator[None]:
            async with inner(key):
                held[0] = True
                try:
                    yield
                finally:
                    held[0] = False

        base.lock = alock
    else:

        @contextlib.contextmanager
        def lock(key: str) -> Iterator[None]:
            with inner(key):
                held[0] = True
                try:
                    yield
                finally:
                    held[0] = False

        base.lock = lock
    base.held = held
    return base


def test_the_last_adopted_resend_holds_the_store_lock(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    """With a store lock, no other refresh can revoke the token of the last resend."""
    store = locking_store(h)
    auth = web_auth()
    org = RotatingOrg(store, auth.token_key, rivals=2)
    org.valid = set()
    mock.post(TOKEN_URL).mock(side_effect=org.token)
    locked: dict[str, bool] = {}

    def api(request: httpx.Request) -> httpx.Response:
        locked[request.headers["Authorization"].removeprefix("Bearer ")] = store.held[0]
        return org.api(request)

    mock.get(URL).mock(side_effect=api)
    sf = h.client(auth, store=store, token=make_token("AT-1", "RT-1"), recorder=rec)
    assert h.run(sf.limits())["DailyApiRequests"].max == 15000
    assert locked == {"AT-1": False, "AT-2": False, "AT-3": False, "AT-4": True}
    assert org.posts == 1
    assert store.held[0] is False
    assert rec.requests[-1]["retry_reasons"] == ["token_superseded", "token_superseded"]
