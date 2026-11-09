"""Schema surprises on 2xx responses must surface as UnexpectedResponse (§8.1, §8.3)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness

LEAD_ID = "00Qxx0000000001AAA"
HTML = httpx.Response(
    200, text="<html>Service temporarily unavailable</html>", headers={"content-type": "text/html"}
)
BAD_JSON = httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"})

CALLS: dict[str, tuple[str, str, Callable[[Any], Any]]] = {
    "query": ("GET", f"{DATA}/query", lambda sf: sf.query("SELECT Id FROM Lead")),
    "count": ("GET", f"{DATA}/query", lambda sf: sf.count("SELECT COUNT() FROM Lead")),
    "search": ("GET", f"{DATA}/search", lambda sf: sf.search("FIND {x}")),
    "limits": ("GET", f"{DATA}/limits", lambda sf: sf.limits()),
    "get": ("GET", f"{DATA}/sobjects/Lead/{LEAD_ID}", lambda sf: sf.sobject("Lead").get(LEAD_ID)),
    "create": ("POST", f"{DATA}/sobjects/Lead/", lambda sf: sf.sobject("Lead").create({"a": 1})),
    "describe": ("GET", f"{DATA}/sobjects/Lead/describe", lambda sf: sf.sobject("Lead").describe()),
    "collections": (
        "POST",
        f"{DATA}/composite/sobjects",
        lambda sf: sf.collections.create([{"a": 1}], sobject="Lead"),
    ),
}


@pytest.mark.parametrize("body", [HTML, BAD_JSON], ids=["html", "bad-json"])
@pytest.mark.parametrize("name", sorted(CALLS))
def test_non_json_success_body(h: Harness, mock: respx.MockRouter, name: str, body: httpx.Response) -> None:
    method, url, call = CALLS[name]
    mock.route(method=method, url=url).mock(return_value=body)
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse) as ei:
        h.run(call(sf))
    assert ei.value.raw


@pytest.mark.parametrize("body", [[], {"totalSize": "x"}, None])
def test_count_with_odd_body(h: Harness, mock: respx.MockRouter, body: Any) -> None:
    mock.get(f"{DATA}/query").respond(json=body)
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.count("SELECT COUNT() FROM Lead"))


def test_request_returns_text_for_non_json(h: Harness, mock: respx.MockRouter) -> None:
    mock.get(f"{DATA}/x").mock(return_value=HTML)
    sf = h.client()
    assert "Service temporarily unavailable" in h.run(sf.request("GET", "x"))
    mock.get(f"{DATA}/y").mock(return_value=BAD_JSON)
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.request("GET", "y"))
