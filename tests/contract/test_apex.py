"""Apex REST: ``sf.apex`` (§9.7, §8.3), on both clients.

Responses are **synthetic** (``tests/fixtures/recorded/apex/*.json`` and inline bodies),
hand-written from the Apex Developer Guide because no org recording exists yet:

- Exposing Apex classes as REST web services (``/services/apexrest/{urlMapping}``; the class
  sets ``RestContext.response.statusCode`` and body; an unhandled exception returns
  ``APEX_ERROR``):
  https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_rest_intro.htm
- RestResponse class (``statusCode``, ``responseBody``, ``addHeader``):
  https://developer.salesforce.com/docs/atlas.en-us.apexref.meta/apexref/apex_methods_system_restresponse.htm
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import INSTANCE, Harness, Recorder
from ..fixtures import load_fixture

APEX = f"{INSTANCE}/services/apexrest"


def test_get_and_post(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    get = mock.get(f"{APEX}/orders/42").respond(json={"id": "42", "state": "open"})
    post = mock.post(f"{APEX}/orders/submit").respond(201, json={"ok": True})
    sf = h.client(recorder=rec)
    assert h.run(sf.apex.get("/orders/42", params={"full": "1"})) == {"id": "42", "state": "open"}
    assert get.calls[0].request.url.params["full"] == "1"
    assert h.run(sf.apex.post("/orders/submit", {"orderId": "42"})) == {"ok": True}
    assert json.loads(post.calls[0].request.content) == {"orderId": "42"}
    assert [e["operation"] for e in rec.requests] == ["apex.get", "apex.post"]
    assert rec.requests[0]["path_template"] == "/services/apexrest/orders/42"


def test_put_patch_delete_and_bodies(h: Harness, mock: respx.MockRouter) -> None:
    mock.put(f"{APEX}/orders/1").respond(text="done")
    mock.patch(f"{APEX}/orders/1").respond(json=[1, 2])
    mock.delete(f"{APEX}/orders/1").respond(204)
    sf = h.client()
    assert h.run(sf.apex.put("/orders/1", {"a": 1})) == "done"
    assert h.run(sf.apex.patch("/orders/1", {"a": 2}, headers={"X-Trace": "t"})) == [1, 2]
    assert h.run(sf.apex.delete("/orders/1")) is None


def test_unhandled_exception_is_apex_error(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{APEX}/orders/submit").respond(500, json=load_fixture("apex/unhandled.json"))
    sf = h.client()
    with pytest.raises(esf.ApexError) as ei:
        h.run(sf.apex.post("/orders/submit", {}))
    assert ei.value.error_code == "APEX_ERROR"
    assert ei.value.status == 500
    assert ei.value.retryable is False
    assert ei.value.body[0]["errorCode"] == "APEX_ERROR"


def test_class_error_body_is_kept(h: Harness, mock: respx.MockRouter) -> None:
    """A class that sets statusCode=422 and returns its own JSON gets ApexError with .body."""
    mock.post(f"{APEX}/orders/submit").respond(422, json={"error": "ORDER_CLOSED", "detail": "closed"})
    sf = h.client()
    with pytest.raises(esf.ApexError) as ei:
        h.run(sf.apex.post("/orders/submit", {"orderId": "1"}))
    assert ei.value.body == {"error": "ORDER_CLOSED", "detail": "closed"}
    assert ei.value.status == 422
    assert ei.value.operation == "apex.post"


def test_unknown_url_mapping_is_apex_error(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{APEX}/nope").respond(404, json=load_fixture("apex/not_found.json"))
    sf = h.client()
    with pytest.raises(esf.ApexError) as ei:
        h.run(sf.apex.get("/nope"))
    assert ei.value.error_code == "NOT_FOUND"


def test_auth_limit_and_gateway_errors_keep_their_class(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{APEX}/limit").respond(
        403, json=[{"errorCode": "REQUEST_LIMIT_EXCEEDED", "message": "TotalRequests Limit exceeded."}]
    )
    mock.get(f"{APEX}/gw").respond(503)
    mock.get(f"{APEX}/html").respond(
        502, text="<html>bad gateway</html>", headers={"content-type": "text/html"}
    )
    sf = h.client(retry=esf.RetryPolicy(max_attempts=1))
    with pytest.raises(esf.RequestLimitExceeded):
        h.run(sf.apex.get("/limit"))
    with pytest.raises(esf.ServerError):
        h.run(sf.apex.get("/gw"))
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.apex.get("/html"))


def test_post_is_not_resent_get_only_with_idempotent(h: Harness, mock: respx.MockRouter) -> None:
    """Apex REST runs code: no resend after a timeout unless the caller says it is safe."""
    post = mock.post(f"{APEX}/run").mock(side_effect=httpx.ReadTimeout("slow"))
    get = mock.get(f"{APEX}/run").mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={})])
    sf = h.client()
    with pytest.raises(esf.RequestTimeout):
        h.run(sf.apex.post("/run", {}))
    assert post.call_count == 1
    assert h.run(sf.apex.get("/run", idempotent=True)) == {}
    assert get.call_count == 2


def test_refreshes_on_expired_session(h: Harness, mock: respx.MockRouter) -> None:
    from ..conftest import TOKEN_URL, token_body

    mock.post(TOKEN_URL).respond(json=token_body("AT-fresh-0002"))
    route = mock.get(f"{APEX}/me").mock(
        side_effect=[
            httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}]),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sf = h.client()
    assert h.run(sf.apex.get("/me")) == {"ok": True}
    assert route.calls[1].request.headers["authorization"] == "Bearer AT-fresh-0002"


def test_non_401_invalid_session_is_not_resent(h: Harness, mock: respx.MockRouter) -> None:
    """Only SOAP reports a rejected session without a 401. On REST a 500 came from code that ran,
    so a POST that returns ``INVALID_SESSION_ID`` with HTTP 500 is not refreshed and resent."""
    from ..conftest import TOKEN_URL, token_body

    token = mock.post(TOKEN_URL).respond(json=token_body("AT-fresh-0002"))
    route = mock.post(f"{APEX}/run").respond(
        500, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}]
    )
    sf = h.client()
    with pytest.raises(esf.SessionExpired):
        h.run(sf.apex.post("/run", {}))
    assert route.call_count == 1
    assert not token.called


@pytest.mark.parametrize("bad", ["orders", "/services/apexrest/orders", "/a?x=1", "/../data"])
def test_bad_paths(h: Harness, mock: respx.MockRouter, bad: str) -> None:
    sf = h.client()
    with pytest.raises(ValueError):
        h.run(sf.apex.get(bad))
    assert not mock.calls
