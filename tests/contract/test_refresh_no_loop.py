"""Guard: a token refresh can never loop, recurse or stampede the token endpoint (both clients).

simple-salesforce regressions this guards against (behaviour only, no code was read or copied):

- #569: a 401 after a successful refresh triggered another refresh, and another, ...
- #804: a token endpoint that itself answered 401 / ``invalid_grant`` was retried or
  recursed into instead of surfacing a typed error.
- #709: many workers hitting an expired session at once each re-authenticated, flooding the
  token endpoint (and tripping login rate limits) when the credentials were bad.

Already covered elsewhere (not repeated here): one caller, refresh OK, retried call 401 again
→ :class:`SessionExpired` with exactly one token POST and two API calls
(``test_refresh.py::test_second_401_raises_session_expired``); ``invalid_grant`` for the
web-server flow (``test_refresh.py::test_invalid_grant_requires_reauthorization``).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, TOKEN_URL, Harness, cc_auth, make_token, token_body, web_auth
from .test_refresh_concurrency import run_concurrently, slow_transport

URL = f"{DATA}/limits"
EXPIRED = httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}])
N = 20


class Counter:
    """Thread-safe call counter that answers with a fixed response (or a sequence of tokens)."""

    def __init__(
        self,
        response: httpx.Response | None = None,
        *,
        fresh_tokens: bool = False,
        wait_for: Any = None,
    ) -> None:
        self.lock = threading.Lock()
        self.count = 0
        self.response = response
        self.fresh_tokens = fresh_tokens
        self.wait_for = wait_for  # (sync only) block until this returns True, max 5 s

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.wait_for is not None:
            deadline = time.monotonic() + 5
            while not self.wait_for() and time.monotonic() < deadline:
                time.sleep(0.001)
        with self.lock:
            self.count += 1
            n = self.count
        if self.fresh_tokens:
            return httpx.Response(200, json=token_body(f"AT-fresh-{n}", "RT-next"))
        assert self.response is not None
        return httpx.Response(
            self.response.status_code, headers=self.response.headers, content=self.response.content
        )


def flows() -> list[Any]:
    return [
        pytest.param(lambda: (cc_auth(), make_token("AT-stale")), id="client_credentials"),
        pytest.param(lambda: (web_auth(), make_token("AT-stale", "RT-1")), id="web_server"),
    ]


# --- (a) refresh succeeds, the API still says 401 ---------------------------------------------


@pytest.mark.parametrize("flow", flows())
def test_each_call_refreshes_at_most_once(h: Harness, mock: respx.MockRouter, flow: Any) -> None:
    """Sequential calls against an API that never accepts a token: one refresh per call, no more."""
    auth, tok = flow()
    api = mock.get(URL).mock(return_value=EXPIRED)
    endpoint = Counter(fresh_tokens=True)
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    sf = h.client(auth, token=tok)
    for i in range(1, 4):
        with pytest.raises(esf.SessionExpired) as ei:
            h.run(sf.limits())
        assert "after one token refresh" in ei.value.message
        assert endpoint.count == i
        assert api.call_count == 2 * i


# --- (b) the token endpoint itself fails ----------------------------------------------------------

TOKEN_FAILURES = [
    pytest.param(
        httpx.Response(401, json={"error": "invalid_client", "error_description": "bad"}), id="401-json"
    ),
    pytest.param(httpx.Response(401), id="401-empty"),
    pytest.param(httpx.Response(401, text="<html>Unauthorized</html>"), id="401-html"),
    pytest.param(
        httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "expired access/refresh token"}
        ),
        id="400-invalid_grant",
    ),
    pytest.param(
        httpx.Response(
            400, json={"error": "invalid_client", "error_description": "invalid client credentials"}
        ),
        id="400-invalid_client",
    ),
    pytest.param(
        httpx.Response(
            400, json={"error": "invalid_client_id", "error_description": "client identifier invalid"}
        ),
        id="400-invalid_client_id",
    ),
]


def expected_error(resp: httpx.Response, auth: Any = None) -> type[esf.SalesforceError]:
    if b"invalid_grant" in resp.content:
        # Only a refresh-token flow needs the user to log in again; for client credentials /
        # JWT it's a configuration problem.
        uses_rt = auth is None or getattr(auth, "uses_refresh_token", False)
        return esf.ReauthorizationRequired if uses_rt else esf.OAuthError
    if b'"error"' in resp.content:
        return esf.OAuthError
    return esf.RefreshFailed


@pytest.mark.parametrize("flow", flows())
@pytest.mark.parametrize("failure", TOKEN_FAILURES)
def test_token_endpoint_failure_is_terminal(
    h: Harness, mock: respx.MockRouter, flow: Any, failure: httpx.Response
) -> None:
    auth, tok = flow()
    api = mock.get(URL).mock(return_value=EXPIRED)
    endpoint = Counter(failure)
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    sf = h.client(auth, token=tok)
    with pytest.raises(esf.AuthError) as ei:
        h.run(sf.limits())
    assert type(ei.value) is expected_error(failure, auth)
    assert ei.value.status == failure.status_code
    assert endpoint.count == 1  # no retry, no recursion into another refresh
    assert api.call_count == 1  # the API call is not resent without a new token


@pytest.mark.parametrize("failure", TOKEN_FAILURES)
def test_initial_acquisition_failure_is_terminal(
    h: Harness, mock: respx.MockRouter, failure: httpx.Response
) -> None:
    api = mock.get(URL).respond(json={})
    endpoint = Counter(failure)
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    sf = h.client(cc_auth(), token=False)
    with pytest.raises(esf.AuthError):
        h.run(sf.limits())
    assert endpoint.count == 1
    assert not api.called


# --- (c) the same under 20 concurrent callers --------------------------------------------------------


def outcomes(h: Harness, calls: list[Any]) -> list[BaseException | Any]:
    """Run ``calls`` concurrently and return each result or raised exception."""

    def wrap(call: Any) -> Any:
        if h.is_async:

            async def go() -> Any:
                try:
                    return await call()
                except esf.SalesforceError as exc:
                    return exc

            return go

        def go_sync() -> Any:
            try:
                return call()
            except esf.SalesforceError as exc:
                return exc

        return go_sync

    return run_concurrently(h, [wrap(c) for c in calls])


@pytest.mark.parametrize("flow", flows())
def test_concurrent_callers_refresh_once_then_fail(h: Harness, mock: respx.MockRouter, flow: Any) -> None:
    auth, tok = flow()
    api = Counter(EXPIRED)
    endpoint = Counter(fresh_tokens=True)
    mock.get(URL).mock(side_effect=api)
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    sf = h.client(auth, token=tok)
    slow_transport(h, sf, 0.01)

    results = outcomes(h, [sf.limits for _ in range(N)])

    assert all(type(r) is esf.SessionExpired for r in results), results
    assert endpoint.count == 1
    assert api.count == 2 * N


@pytest.mark.parametrize("flow", flows())
@pytest.mark.parametrize("failure", TOKEN_FAILURES)
def test_concurrent_callers_share_one_failed_refresh(
    h: Harness, mock: respx.MockRouter, flow: Any, failure: httpx.Response
) -> None:
    auth, tok = flow()
    api = Counter(EXPIRED)
    # Threads: hold the token response until every caller has seen its 401, so all of them are
    # genuinely waiting on the in-flight refresh (a caller arriving after the failure was recorded
    # is a "later call" and may legitimately try again).
    endpoint = Counter(failure, wait_for=None if h.is_async else (lambda: api.count >= N))
    mock.get(URL).mock(side_effect=api)
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    sf = h.client(auth, token=tok)
    slow_transport(h, sf, 0.01)

    results = outcomes(h, [sf.limits for _ in range(N)])

    cls = expected_error(failure, auth)
    assert all(type(r) is cls for r in results), results
    assert len({id(r) for r in results}) == N  # each caller gets its own exception object
    assert endpoint.count == 1  # one failed refresh is shared, not repeated per waiter
    assert api.count == N


@pytest.mark.parametrize("failure", TOKEN_FAILURES)
def test_concurrent_initial_acquisition_shares_one_failure(
    h: Harness, mock: respx.MockRouter, failure: httpx.Response
) -> None:
    api = Counter(httpx.Response(200, json={}))
    started = Counter()

    def all_waiting() -> bool:
        # Threads: hold the token response until every caller has started (plus a moment to
        # reach the lock), so none arrives after the failure and legitimately tries again.
        if started.count < N:
            return False
        time.sleep(0.05)
        return True

    endpoint = Counter(failure, wait_for=None if h.is_async else all_waiting)
    mock.get(URL).mock(side_effect=api)
    mock.post(TOKEN_URL).mock(side_effect=endpoint)
    sf = h.client(cc_auth(), token=False)
    slow_transport(h, sf, 0.01)

    def call() -> Any:
        with started.lock:
            started.count += 1
        return sf.limits()

    results = outcomes(h, [call if not h.is_async else sf.limits for _ in range(N)])

    assert all(isinstance(r, esf.AuthError) for r in results), results
    assert endpoint.count == 1
    assert api.count == 0


def test_a_later_call_may_try_again(h: Harness, mock: respx.MockRouter) -> None:
    """Sharing a failure is for concurrent waiters only: a later call makes a fresh attempt."""
    api = mock.get(URL)
    api.side_effect = [EXPIRED, EXPIRED, httpx.Response(200, json={})]
    tok = mock.post(TOKEN_URL)
    tok.side_effect = [
        httpx.Response(400, json={"error": "invalid_client", "error_description": "bad"}),
        httpx.Response(200, json=token_body("AT-2")),
    ]
    sf = h.client(cc_auth(), token=make_token("AT-stale"))
    with pytest.raises(esf.OAuthError):
        h.run(sf.limits())
    assert h.run(sf.limits()) == {}
    assert tok.call_count == 2
