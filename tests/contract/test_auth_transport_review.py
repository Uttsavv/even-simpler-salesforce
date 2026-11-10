"""Review fixes in auth and the transport, for both clients.

- The ``instance_url`` of a token from the token endpoint or a token store is checked before
  the bearer token is sent to it.
- A ``401`` is answered with a refresh and a resend only if Salesforce rejected the token, or
  the request is a read (ADR 0001).
- ``max_concurrency`` is not held while a call sleeps before a retry.

All responses here are synthetic (hand-written), not recorded.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, TOKEN_URL, Harness, Recorder, cc_auth, make_token, token_body

APEX_URL = f"{INSTANCE}/services/apexrest/orders"
LIMITS_URL = f"{DATA}/limits"
EXPIRED = httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}])


def bearer(request: httpx.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


# --- instance_url from the token endpoint and the token store -------------------------------------


@pytest.mark.parametrize("bad", ["http://attacker.example", "ftp://acme.my.salesforce.com"])
def test_token_response_with_unsafe_instance_url_is_rejected(
    h: Harness, mock: respx.MockRouter, bad: str
) -> None:
    """synthetic: a token response that points the bearer token at plain http."""
    body = {**token_body("AT-evil"), "instance_url": bad}
    tok = mock.post(TOKEN_URL).respond(json=body)
    attacker = mock.route(host="attacker.example").respond(json={})
    store = h.new_store()
    sf = h.client(store=store, token=False)
    with pytest.raises(esf.UnexpectedResponse, match="instance_url"):
        h.run(sf.limits())
    assert tok.call_count == 1
    assert not attacker.called
    assert h.load(store, cc_auth().token_key) is None  # never saved


def test_refreshed_token_with_unsafe_instance_url_is_rejected(h: Harness, mock: respx.MockRouter) -> None:
    """synthetic: the refresh after a 401 returns an http instance_url."""
    api = mock.get(LIMITS_URL).mock(return_value=EXPIRED)
    mock.post(TOKEN_URL).respond(json={**token_body("AT-evil"), "instance_url": "http://attacker.example"})
    attacker = mock.route(host="attacker.example").respond(json={})
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.limits())
    assert api.call_count == 1
    assert not attacker.called


def test_stored_token_with_unsafe_instance_url_is_rejected(h: Harness, mock: respx.MockRouter) -> None:
    """A token store that was tampered with can't redirect the bearer token."""
    attacker = mock.route(host="attacker.example").respond(json={})
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-2"))
    sf = h.client(token=make_token(instance_url="http://attacker.example"))
    with pytest.raises(esf.AuthError, match="instance_url") as ei:
        h.run(sf.limits())
    assert "AT-initial" not in str(ei.value)
    assert not attacker.called
    assert not tok.called


def test_stored_loopback_http_token_is_allowed(h: Harness, mock: respx.MockRouter) -> None:
    api = mock.get("http://localhost:8080/services/data/v68.0/limits").respond(json={})
    sf = h.client(token=make_token(instance_url="http://localhost:8080"))
    assert h.run(sf.limits()) == {}
    assert api.call_count == 1


# --- 401: only a rejected token is refreshed and resent for writes --------------------------------


def test_apex_rest_401_post_is_not_resent(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """synthetic: an Apex REST class that ran and then returned 401 itself."""
    api = mock.post(APEX_URL).respond(401, json={"message": "customer PIN invalid"})
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-2"))
    sf = h.client(recorder=rec)
    with pytest.raises(esf.SessionExpired) as ei:
        h.run(sf.request("POST", "/services/apexrest/orders", json={"pin": "0000"}))
    assert ei.value.message == "customer PIN invalid"
    assert api.call_count == 1
    assert not tok.called
    assert rec.requests[-1]["refreshed"] is False
    assert rec.requests[-1]["attempts"] == 1


@pytest.mark.parametrize(
    "rejected",
    [
        pytest.param(EXPIRED, id="INVALID_SESSION_ID"),
        pytest.param(httpx.Response(401, json={"error": "invalid_token"}), id="invalid_token"),
    ],
)
def test_post_with_rejected_token_refreshes_and_resends(
    h: Harness, mock: respx.MockRouter, rejected: httpx.Response
) -> None:
    """Salesforce rejects an expired token before running anything: a write is resent."""

    def handler(request: httpx.Request) -> httpx.Response:
        return rejected if bearer(request) == "AT-initial-0001" else httpx.Response(200, json={"ok": True})

    api = mock.post(APEX_URL).mock(side_effect=handler)
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-2"))
    sf = h.client()
    assert h.run(sf.request("POST", "/services/apexrest/orders", json={})) == {"ok": True}
    assert [bearer(c.request) for c in api.calls] == ["AT-initial-0001", "AT-2"]
    assert tok.call_count == 1


def test_get_with_other_401_still_refreshes(h: Harness, mock: respx.MockRouter) -> None:
    """synthetic: a read is safe to resend, so any 401 triggers one refresh."""

    def handler(request: httpx.Request) -> httpx.Response:
        if bearer(request) == "AT-initial-0001":
            return httpx.Response(401)
        return httpx.Response(200, json={"a": 1})

    mock.get(LIMITS_URL).mock(side_effect=handler)
    tok = mock.post(TOKEN_URL).respond(json=token_body("AT-2"))
    sf = h.client()
    assert h.run(sf.request("GET", "limits")) == {"a": 1}
    assert tok.call_count == 1


# --- max_concurrency is released during a retry sleep -------------------------------------------


def _gate_free(gate: Any) -> bool:
    if hasattr(gate, "locked"):  # asyncio.Semaphore
        return not gate.locked()
    if gate.acquire(blocking=False):  # threading.BoundedSemaphore
        gate.release()
        return True
    return False


def test_retry_sleep_does_not_hold_the_concurrency_gate(h: Harness, mock: respx.MockRouter) -> None:
    """synthetic: a 503 then a 200 for a read, with max_concurrency=1."""
    unavailable = httpx.Response(503, json=[{"errorCode": "SERVER_UNAVAILABLE", "message": "x"}])
    api = mock.get(LIMITS_URL).mock(side_effect=[unavailable, httpx.Response(200, json={})])
    sf = h.client(max_concurrency=1)
    gate = sf._transport.gate
    seen: list[bool] = []
    if h.is_async:

        async def sleep(_: float) -> None:
            seen.append(_gate_free(gate))

    else:

        def sleep(_: float) -> None:  # type: ignore[misc]
            seen.append(_gate_free(gate))

    sf._transport.sleep = sleep
    assert h.run(sf.limits()) == {}
    assert api.call_count == 2
    assert seen == [True]
    assert _gate_free(gate)
