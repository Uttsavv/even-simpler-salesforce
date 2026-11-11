"""``sf.request`` events report the real outcome, and error messages stay out of logs."""

from __future__ import annotations

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder

LEAD = "00Q000000000001AAA"


def test_non_json_2xx_is_an_error_in_the_event(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{DATA}/sobjects/Lead/{LEAD}").respond(
        200, text="<html>maintenance</html>", headers={"content-type": "text/html"}
    )
    sf = h.client(recorder=rec)
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.sobject("Lead").get(LEAD))
    ev = rec.requests[-1]
    assert ev["outcome"] == "error"
    assert ev["error_class"] == "InvalidJSONResponse"


def test_raw_request_accepts_non_json(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{DATA}/x").mock(return_value=httpx.Response(200, text="hello"))
    sf = h.client(recorder=rec)
    assert h.run(sf.request("GET", "x")) == "hello"
    assert rec.requests[-1]["outcome"] == "ok"


def test_raw_request_invalid_json_is_an_error_in_the_event(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    # synthetic: a 2xx that says application/json but carries a truncated body
    mock.get(f"{DATA}/x").mock(
        return_value=httpx.Response(200, content=b'{"a": ', headers={"content-type": "application/json"})
    )
    sf = h.client(recorder=rec)
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.request("GET", "x"))
    ev = rec.requests[-1]
    assert ev["outcome"] == "error"
    assert ev["error_class"] == "InvalidJSONResponse"


def test_dml_error_message_only_with_debug(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    body = [{"errorCode": "STRING_TOO_LONG", "message": "Name: data value too large: SECRET-VALUE"}]
    mock.patch(f"{DATA}/sobjects/Lead/{LEAD}").respond(400, json=body)
    sf = h.client(recorder=rec)
    with pytest.raises(esf.StringTooLong) as ei:
        h.run(sf.sobject("Lead").update(LEAD, {"Name": "SECRET-VALUE"}))
    assert "SECRET-VALUE" in ei.value.message
    assert rec.requests[-1]["error_message"] is None
    sf._transport.debug = 1
    with pytest.raises(esf.StringTooLong):
        h.run(sf.sobject("Lead").update(LEAD, {"Name": "x"}))
    assert rec.requests[-1]["error_message"] == body[0]["message"]
