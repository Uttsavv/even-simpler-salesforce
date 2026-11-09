"""``sf.request`` response handling, and no ``X-PrettyPrint`` on any request (both clients).

simple-salesforce regressions this guards against (behaviour only, no code was read or
copied):

- #146, #122: 2xx statuses other than 200 (201, 202, 204, 206) treated as errors, or an
  empty 204 body parsed as JSON and raising.
- #501, #386: non-JSON bodies (CSV, HTML, plain text) from raw/Apex REST calls raising a JSON
  decode error instead of being returned.
- #596: ``X-PrettyPrint: 1`` sent on every request, inflating responses.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, INSTANCE, Harness

URL = f"{DATA}/x"
LEAD_ID = "00Qxx0000000001AAA"


@pytest.mark.parametrize("status", [200, 201, 202, 204, 206])
def test_every_2xx_is_success(h: Harness, mock: respx.MockRouter, status: int) -> None:
    resp = httpx.Response(status) if status == 204 else httpx.Response(status, json={"status": status})
    mock.post(URL).mock(return_value=resp)
    sf = h.client()
    out = h.run(sf.request("POST", "x", json={"a": 1}))
    assert out == (None if status == 204 else {"status": status})


@pytest.mark.parametrize(
    "resp",
    [
        pytest.param(httpx.Response(204), id="204"),
        pytest.param(httpx.Response(200), id="200-empty"),
        pytest.param(httpx.Response(200, headers={"content-type": "application/json"}), id="200-empty-json"),
        pytest.param(httpx.Response(202, content=b""), id="202-empty"),
    ],
)
def test_empty_bodies_are_none(h: Harness, mock: respx.MockRouter, resp: httpx.Response) -> None:
    mock.get(URL).mock(return_value=resp)
    sf = h.client()
    assert h.run(sf.request("GET", "x")) is None


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", "plain text"),
        ("text/plain; charset=utf-8", "café ✓"),
        ("text/csv", '"Id","Name"\n"001","Acme"\n'),
        ("text/html; charset=UTF-8", "<html><body>maintenance</body></html>"),
        ("application/xml", "<?xml version='1.0'?><r/>"),
        ("application/octet-stream", "binary-ish"),
        (None, '{"looks": "like json but unlabelled"}'),
    ],
)
def test_non_json_bodies_are_str(
    h: Harness, mock: respx.MockRouter, content_type: str | None, body: str
) -> None:
    headers = {"content-type": content_type} if content_type else {}
    mock.get(URL).mock(return_value=httpx.Response(200, content=body.encode(), headers=headers))
    sf = h.client()
    out = h.run(sf.request("GET", "x"))
    assert out == body
    assert isinstance(out, str)


def test_invalid_utf8_non_json_body_does_not_raise(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(URL).respond(200, content=b"\xff\xfeok", headers={"content-type": "text/plain"})
    sf = h.client()
    out = h.run(sf.request("GET", "x"))
    assert isinstance(out, str)
    assert out.endswith("ok")


def test_body_labelled_json_but_invalid_is_a_typed_error(h: Harness, mock: respx.MockRouter) -> None:
    """Salesforce said JSON and sent something else: a typed error, not a bare JSONDecodeError."""
    mock.get(URL).respond(200, content=b"<html>proxy</html>", headers={"content-type": "application/json"})
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse) as ei:
        h.run(sf.request("GET", "x"))
    assert ei.value.status == 200


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/json; charset=UTF-8",
        "application/json;charset=utf-8",
        "Application/JSON; Charset=UTF-8",
        "application/vnd.salesforce+json; charset=utf-8",
    ],
)
def test_json_with_parameters_is_parsed(h: Harness, mock: respx.MockRouter, content_type: str) -> None:
    payload = {"Name": "Café über ✓", "n": [1, 2]}
    mock.get(URL).respond(
        200, content=json.dumps(payload, ensure_ascii=False).encode(), headers={"content-type": content_type}
    )
    sf = h.client()
    assert h.run(sf.request("GET", "x")) == payload


# --- no X-PrettyPrint, anywhere -------------------------------------------------------------------


def answer(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    method = request.method
    if path.endswith("/query"):
        return httpx.Response(200, json={"totalSize": 0, "done": True, "records": []})
    if path.endswith("/limits"):
        return httpx.Response(200, json={"DailyApiRequests": {"Max": 10, "Remaining": 5}})
    if path.endswith("/composite/sobjects") and method == "POST":
        n = len(json.loads(request.content)["records"])
        return httpx.Response(200, json=[{"id": LEAD_ID, "success": True, "errors": []}] * n)
    if path.endswith("/composite/sobjects") and method == "DELETE":
        return httpx.Response(200, json=[{"id": LEAD_ID, "success": True, "errors": []}])
    if path.endswith("/describe"):
        return httpx.Response(200, json={"name": "Lead", "fields": []})
    if method == "POST" and path.endswith("/sobjects/Lead/"):
        return httpx.Response(201, json={"id": LEAD_ID, "success": True, "errors": []})
    if method in ("PATCH", "DELETE"):
        return httpx.Response(204)
    return httpx.Response(200, json={"Id": LEAD_ID})


def test_no_request_carries_x_prettyprint(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.route(url__startswith=INSTANCE).mock(side_effect=answer)
    sf = h.client()
    lead = sf.sobject("Lead")
    calls: list[Any] = [
        lambda: sf.query("SELECT Id FROM Lead"),
        lambda: sf.limits(),
        lambda: lead.get(LEAD_ID),
        lambda: lead.create({"LastName": "x"}),
        lambda: lead.update(LEAD_ID, {"LastName": "y"}),
        lambda: lead.delete(LEAD_ID),
        lambda: lead.describe(),
        lambda: sf.collections.create([{"LastName": "z"}], sobject="Lead"),
        lambda: sf.collections.delete([LEAD_ID]),
        lambda: sf.request("GET", "sobjects/Lead/x"),
        lambda: sf.request("POST", "/services/apexrest/thing", json={"a": 1}),
    ]
    for call in calls:
        h.run(call())
    assert route.call_count == len(calls)
    for c in route.calls:
        req = c.request
        assert "x-prettyprint" not in {k.lower() for k in req.headers}, req.url
        assert "prettyprint" not in {k.lower() for k in req.url.params}, req.url
