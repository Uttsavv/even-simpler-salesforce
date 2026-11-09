"""Retry policy through the full pipeline (§7.4) with a fake clock."""

from __future__ import annotations

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture

LEAD_ID = "00Qxx0000000001AAA"
GET_URL = f"{DATA}/sobjects/Lead/{LEAD_ID}"
POST_URL = f"{DATA}/sobjects/Lead/"
OK_GET = httpx.Response(200, json={"Id": LEAD_ID})
OK_POST = httpx.Response(201, json={"id": LEAD_ID})


def get(h: Harness, sf: object) -> object:
    return h.run(sf.sobject("Lead").get(LEAD_ID))  # type: ignore[attr-defined]


def post(h: Harness, sf: object) -> object:
    return h.run(sf.sobject("Lead").create({"LastName": "x"}))  # type: ignore[attr-defined]


# --- connect errors ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("t"), httpx.PoolTimeout("p")]
)
def test_connect_error_retried_even_for_post(
    h: Harness, mock: respx.MockRouter, rec: Recorder, exc: Exception
) -> None:
    route = mock.post(POST_URL)
    route.side_effect = [exc, exc, OK_POST]
    sf = h.client(recorder=rec)
    assert post(h, sf) == LEAD_ID
    assert route.call_count == 3
    assert sf.sleeps == [0.25, 0.5]
    retries = rec.named("sf.retry")
    assert [(e["attempt"], e["reason"], e["delay_ms"]) for e in retries] == [
        (1, "connect_error", 250.0),
        (2, "connect_error", 500.0),
    ]
    ev = rec.requests[-1]
    assert ev["attempts"] == 3
    assert ev["retry_reasons"] == ["connect_error", "connect_error"]
    assert ev["outcome"] == "ok"
    assert sf.usage.snapshot().retries == 2


def test_connect_error_exhausts_attempts(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(GET_URL).mock(side_effect=httpx.ConnectError("refused"))
    sf = h.client(
        recorder=rec, retry=esf.RetryPolicy(max_attempts=4, jitter=False, base_delay=1, max_delay=3)
    )
    with pytest.raises(esf.ConnectError) as ei:
        get(h, sf)
    assert route.call_count == 4
    assert sf.sleeps == [1, 2, 3]
    assert ei.value.retryable is True
    ev = rec.requests[-1]
    assert ev["outcome"] == "error"
    assert ev["error_class"] == "ConnectError"
    assert ev["status"] is None
    assert ev["attempts"] == 4


def test_max_attempts_one_disables_retries(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(GET_URL).mock(side_effect=httpx.ConnectError("refused"))
    sf = h.client(retry=esf.RetryPolicy(max_attempts=1))
    with pytest.raises(esf.ConnectError):
        get(h, sf)
    assert route.call_count == 1
    assert sf.sleeps == []


# --- read timeouts / resets after send -------------------------------------------------------------


def test_read_timeout_retried_for_get(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(GET_URL)
    route.side_effect = [httpx.ReadTimeout("slow"), OK_GET]
    sf = h.client()
    assert get(h, sf) == {"Id": LEAD_ID}
    assert route.call_count == 2


def test_read_timeout_not_retried_for_post(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.post(POST_URL)
    route.side_effect = [httpx.ReadTimeout("slow"), OK_POST]
    sf = h.client(recorder=rec)
    with pytest.raises(esf.RequestTimeout):
        post(h, sf)
    assert route.call_count == 1
    assert sf.sleeps == []
    assert rec.requests[-1]["error_class"] == "RequestTimeout"


def test_read_timeout_retried_for_idempotent_post(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/actions/x")
    route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json={"ok": True})]
    sf = h.client()
    assert h.run(sf.request("POST", "actions/x", json={}, idempotent=True)) == {"ok": True}
    assert route.call_count == 2


def test_connection_reset_after_send(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(POST_URL).mock(side_effect=httpx.RemoteProtocolError("reset"))
    sf = h.client()
    with pytest.raises(esf.TransportError) as ei:
        post(h, sf)
    assert type(ei.value) is esf.TransportError
    assert route.call_count == 1
    route2 = mock.get(GET_URL)
    route2.side_effect = [httpx.ReadError("reset"), OK_GET]
    assert get(h, sf) == {"Id": LEAD_ID}


# --- 5xx --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_retried_for_get_not_post(h: Harness, mock: respx.MockRouter, status: int) -> None:
    g = mock.get(GET_URL)
    g.side_effect = [httpx.Response(status), OK_GET]
    p = mock.post(POST_URL)
    p.side_effect = [httpx.Response(status), OK_POST]
    sf = h.client()
    assert get(h, sf) == {"Id": LEAD_ID}
    assert g.call_count == 2
    with pytest.raises(esf.ServerError) as ei:
        post(h, sf)
    assert p.call_count == 1
    assert ei.value.status == status


def test_server_unavailable_body_and_html_503(h: Harness, mock: respx.MockRouter) -> None:
    g = mock.get(GET_URL)
    g.side_effect = [
        httpx.Response(503, json=load_fixture("errors/server_unavailable.json")),
        httpx.Response(503, text="<html>maintenance</html>", headers={"content-type": "text/html"}),
        OK_GET,
    ]
    sf = h.client()
    assert get(h, sf) == {"Id": LEAD_ID}
    assert g.call_count == 3


def test_420_not_retried(h: Harness, mock: respx.MockRouter) -> None:
    g = mock.get(GET_URL).respond(420)
    sf = h.client()
    with pytest.raises(esf.ServerError):
        get(h, sf)
    assert g.call_count == 1


def test_5xx_exhausted(h: Harness, mock: respx.MockRouter) -> None:
    g = mock.get(GET_URL).respond(503)
    sf = h.client()
    with pytest.raises(esf.ServerError):
        get(h, sf)
    assert g.call_count == 3
    assert sf.sleeps == [0.25, 0.5]


# --- rate limits ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(403, json=load_fixture("errors/request_limit.json"), headers={"Retry-After": "2"}),
    ],
    ids=["429", "REQUEST_LIMIT_EXCEEDED"],
)
def test_rate_limit_with_retry_after(
    h: Harness, mock: respx.MockRouter, rec: Recorder, first: httpx.Response
) -> None:
    p = mock.post(POST_URL)
    p.side_effect = [first, OK_POST]
    sf = h.client(recorder=rec)
    assert post(h, sf) == LEAD_ID  # even POST: the request was rejected
    assert sf.sleeps == [2.0]
    assert rec.named("sf.retry")[0]["reason"] == "rate_limited"


@pytest.mark.parametrize(
    "first",
    [httpx.Response(429), httpx.Response(403, json=load_fixture("errors/request_limit.json"))],
    ids=["429", "REQUEST_LIMIT_EXCEEDED"],
)
def test_rate_limit_without_retry_after_raises(
    h: Harness, mock: respx.MockRouter, first: httpx.Response
) -> None:
    g = mock.get(GET_URL)
    g.side_effect = [first, OK_GET]
    sf = h.client()
    with pytest.raises(esf.RequestLimitExceeded) as ei:
        get(h, sf)
    assert ei.value.retry_after is None
    assert g.call_count == 1


def test_retry_after_longer_than_cap_raises(h: Harness, mock: respx.MockRouter) -> None:
    g = mock.get(GET_URL).respond(429, headers={"Retry-After": "120"})
    sf = h.client()
    with pytest.raises(esf.RequestLimitExceeded) as ei:
        get(h, sf)
    assert ei.value.retry_after == 120
    assert g.call_count == 1
    assert sf.sleeps == []


def test_retry_after_ignored_when_disabled(h: Harness, mock: respx.MockRouter) -> None:
    g = mock.get(GET_URL).respond(429, headers={"Retry-After": "1"})
    sf = h.client(retry=esf.RetryPolicy(respect_retry_after=False, jitter=False))
    with pytest.raises(esf.RequestLimitExceeded):
        get(h, sf)
    assert g.call_count == 1


# --- row locks --------------------------------------------------------------------------------------


def test_unable_to_lock_row_retried_on_post(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    p = mock.post(POST_URL)
    p.side_effect = [httpx.Response(400, json=load_fixture("errors/row_locked.json")), OK_POST]
    sf = h.client(recorder=rec)
    assert post(h, sf) == LEAD_ID
    assert p.call_count == 2
    assert rec.requests[-1]["retry_reasons"] == ["row_locked"]
    assert rec.requests[-1]["status"] == 201


def test_unable_to_lock_row_exhausted(h: Harness, mock: respx.MockRouter) -> None:
    p = mock.post(POST_URL).respond(400, json=load_fixture("errors/row_locked.json"))
    sf = h.client()
    with pytest.raises(esf.RowLocked):
        post(h, sf)
    assert p.call_count == 3


def test_other_4xx_never_retried(h: Harness, mock: respx.MockRouter) -> None:
    g = mock.get(GET_URL).respond(400, json=load_fixture("errors/invalid_field.json"))
    sf = h.client()
    with pytest.raises(esf.InvalidField):
        get(h, sf)
    assert g.call_count == 1


def test_jitter_uses_full_jitter_range(h: Harness, mock: respx.MockRouter) -> None:
    g = mock.get(GET_URL)
    g.side_effect = [httpx.Response(503), httpx.Response(503), OK_GET]
    sf = h.client(retry=esf.RetryPolicy(jitter=True, base_delay=1.0, max_delay=8.0))
    get(h, sf)
    assert 0 <= sf.sleeps[0] <= 1.0
    assert 0 <= sf.sleeps[1] <= 2.0
