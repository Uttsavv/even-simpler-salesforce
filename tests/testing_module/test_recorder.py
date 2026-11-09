"""record() / replay(): redacted fixtures, sync + async, through the real client."""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

import httpx
import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import Redactor, ReplayMismatch, record, replay
from even_simpler_salesforce.testing.recorder import load_fixture

INSTANCE = "https://acme-dev-ed.my.salesforce.com"
SESSION = "00D5g000004ABCD!AQ4AQFakeSessionToken.abc_123"
LEAD_ID = "00Q5g00000ABCDEFGH"


def _org(request: httpx.Request) -> httpx.Response:
    """A tiny fake org used as the 'real' upstream."""
    path = request.url.path
    if path == "/services/oauth2/token":
        return httpx.Response(
            200,
            json={
                "access_token": SESSION,
                "instance_url": INSTANCE,
                "id": "https://login.salesforce.com/id/00D5g000004ABCDEAA/0055g00000XYZABAAA",
                "token_type": "Bearer",
                "issued_at": "1700000000000",
                "signature": "c2lnbmF0dXJl",
            },
        )
    if path.endswith("/query"):
        return httpx.Response(
            200,
            headers={"Sforce-Limit-Info": "api-usage=5/15000", "Set-Cookie": "sid=secret"},
            json={
                "totalSize": 1,
                "done": True,
                "records": [
                    {
                        "attributes": {
                            "type": "Lead",
                            "url": f"/services/data/v68.0/sobjects/Lead/{LEAD_ID}",
                        },
                        "Id": LEAD_ID,
                        "Email": "jane.doe@acme.com",
                        "Phone": "(415) 555-1234",
                        "Website": f"{INSTANCE}/x",
                        "Description": f"session {SESSION} and Bearer abc.def",
                    }
                ],
            },
        )
    if path.endswith(f"/sobjects/Lead/{LEAD_ID}") and request.method == "PATCH":
        return httpx.Response(204)
    if path.endswith("/sobjects/Lead/"):
        return httpx.Response(201, json={"id": LEAD_ID, "success": True, "errors": []})
    if path == "/services/apexrest/bin":
        return httpx.Response(
            200, content=b"\x00\xff\x10", headers={"content-type": "application/octet-stream"}
        )
    return httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": "nope"}])


def _auth() -> esf.ClientCredentials:
    return esf.ClientCredentials(domain=INSTANCE, client_id="cid", client_secret="super-secret-value")


def _exercise_sync(transport: httpx.BaseTransport) -> str:
    with esf.Salesforce(_auth(), http_client=httpx.Client(transport=transport)) as sf:
        page = sf.query(esf.soql("SELECT Id, Email FROM Lead WHERE Email = {}", "jane.doe@acme.com"))
        lead_id = page.records[0]["Id"]
        sf.sobject("Lead").update(lead_id, {"Status": "Working", "Email": "jane.doe@acme.com"})
        return lead_id


def test_record_redacts_and_replays_sync(tmp_path: Path) -> None:
    path = tmp_path / "fx" / "leads.json"
    rec = record(path, httpx.MockTransport(_org))
    _exercise_sync(rec)
    text = path.read_text()
    for secret in (
        SESSION,
        "super-secret-value",
        "c2lnbmF0dXJl",
        "jane.doe@acme.com",
        "555-1234",
        "acme-dev-ed",
        "sid=secret",
        "abc.def",
    ):
        assert secret not in text, secret
    doc = json.loads(text)
    assert doc["esf_fixture"] == 1
    inter = doc["interactions"]
    assert [i["request"]["method"] for i in inter] == ["POST", "GET", "PATCH"]
    token_req, query, patch = inter
    form = dict(token_req["request"]["form"])
    assert form["client_secret"] == "<redacted>"
    assert form["grant_type"] == "client_credentials"
    assert token_req["response"]["json"]["access_token"] == "<redacted>"
    assert token_req["response"]["json"]["instance_url"] == "https://example.my.salesforce.com"
    assert "authorization" not in query["request"]["headers"]
    assert query["response"]["headers"]["sforce-limit-info"] == "api-usage=5/15000"
    assert "set-cookie" not in query["response"]["headers"]
    row = query["response"]["json"]["records"][0]
    assert row["Email"] == "user1@example.com"
    assert row["Phone"].startswith("555-01")
    assert "<redacted>" in row["Description"]
    assert dict(query["request"]["query"])["q"].endswith("'user1@example.com'")
    assert row["Id"] == LEAD_ID  # ids kept by default
    assert patch["request"]["json"]["Email"] == "user1@example.com"
    assert patch["response"]["status"] == 204
    assert "json" not in patch["response"]

    # replay through the real client: no network, same results
    assert _exercise_sync(replay(path)) == LEAD_ID


def test_record_and_replay_async(tmp_path: Path) -> None:
    path = tmp_path / "async.json"

    async def run(transport: httpx.AsyncBaseTransport) -> int:
        async with esf.AsyncSalesforce(_auth(), http_client=httpx.AsyncClient(transport=transport)) as sf:
            lead_id = await sf.sobject("Lead").create({"LastName": "Doe"})
            assert lead_id == LEAD_ID
            page = await sf.query("SELECT Id FROM Lead")
            return page.total_size

    rec = record(path, httpx.MockTransport(_org))
    assert asyncio.run(run(rec)) == 1
    assert len(load_fixture(path)) == 3
    assert asyncio.run(run(replay(path))) == 1


def test_mask_ids_is_consistent(tmp_path: Path) -> None:
    path = tmp_path / "ids.json"
    _exercise_sync(record(path, httpx.MockTransport(_org), mask_ids=True))
    text = path.read_text()
    assert LEAD_ID not in text
    assert "00D5g000004ABCD" not in text
    doc = json.loads(text)
    masked = doc["interactions"][1]["response"]["json"]["records"][0]["Id"]
    assert masked.startswith("00Q")
    assert len(masked) == 18
    assert doc["interactions"][2]["request"]["path"].endswith(f"/sobjects/Lead/{masked}")
    # the replayed client follows the masked id and still matches the recorded PATCH
    assert _exercise_sync(replay(path)) == masked


def test_binary_text_and_gzip_bodies(tmp_path: Path) -> None:
    path = tmp_path / "bin.json"
    big = {"records": [{"attributes": {"type": "Lead"}, "LastName": "x" * 100}] * 800}
    with esf.Salesforce(
        _auth(), http_client=httpx.Client(transport=record(path, httpx.MockTransport(_org)))
    ) as sf:
        assert sf.request("GET", "/services/apexrest/bin") == "\x00�\x10"
        with pytest.raises(esf.NotFound):
            sf.request(
                "POST",
                "/services/apexrest/missing",
                content=b"plain text",
                headers={"Content-Type": "text/plain"},
            )
        with pytest.raises(esf.NotFound):
            sf.collections.create(big["records"])  # gzip-compressed request (>64 KiB)
    inter = load_fixture(path)
    assert "base64" in inter[1]["response"]
    assert inter[2]["request"]["text"] == "plain text"
    assert inter[3]["request"]["headers"]["content-type"] == "application/json"
    assert len(inter[3]["request"]["json"]["records"]) == 200
    with esf.Salesforce(_auth(), http_client=httpx.Client(transport=replay(path))) as sf:
        assert sf.request("GET", "/services/apexrest/bin") == "\x00�\x10"


def test_replay_mismatch_and_options(tmp_path: Path) -> None:
    path = tmp_path / "one.json"
    path.write_text(
        json.dumps(
            {
                "esf_fixture": 1,
                "interactions": [
                    {
                        "request": {"method": "GET", "path": "/a", "query": [["x", "1"]]},
                        "response": {"status": 200, "json": {"n": 1}},
                    },
                    {
                        "request": {"method": "GET", "path": "/a", "query": [["x", "2"]]},
                        "response": {"status": 200, "text": "two", "headers": {"content-type": "text/plain"}},
                    },
                    {
                        "request": {"method": "POST", "path": "/f"},
                        "response": {
                            "status": 200,
                            "form": [["a", "b"]],
                            "headers": {"content-type": "application/x-www-form-urlencoded"},
                        },
                    },
                ],
            }
        )
    )
    t = replay(path)
    with httpx.Client(transport=t) as c:
        assert c.get("https://h/a").json() == {"n": 1}
        assert c.get("https://h/a").text == "two"
        assert c.post("https://h/f").text == "a=b"
        with pytest.raises(ReplayMismatch, match="GET /a"):
            c.get("https://h/a")
    assert t.handler.remaining == 0  # type: ignore[attr-defined]
    with httpx.Client(transport=replay(path, match_query=True, repeat=True)) as c:
        assert c.get("https://h/a?x=2").text == "two"
        assert c.get("https://h/a?x=2").text == "two"
        with pytest.raises(ReplayMismatch):
            c.get("https://h/a?x=3")


def test_load_fixture_validates(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"interactions": []}))
    with pytest.raises(ValueError, match="not an esf fixture"):
        replay(bad)
    bad.write_text(json.dumps({"esf_fixture": 1, "interactions": {}}))
    with pytest.raises(ValueError, match="must be a list"):
        replay(bad)


def test_recording_transport_mode_errors(tmp_path: Path) -> None:
    sync_only = record(tmp_path / "a.json", httpx.HTTPTransport())
    with pytest.raises(TypeError, match="sync-only"):
        asyncio.run(sync_only.handle_async_request(httpx.Request("GET", "https://h/")))
    sync_only.close()
    async_only = record(tmp_path / "b.json", httpx.AsyncHTTPTransport())
    with pytest.raises(TypeError, match="async-only"):
        async_only.handle_request(httpx.Request("GET", "https://h/"))
    asyncio.run(async_only.aclose())
    assert load_fixture(tmp_path / "a.json") == []


def test_redactor_unit() -> None:
    r = Redactor(mask_ids=True, extra_keys={"api_key"})
    out = r.value(
        {
            "api_key": "k",
            "refresh_token": "5Aep",
            "nested": [{"MobilePhone": "+1 415 555 0000", "Fax": ""}],
            "note": "mail me: a@b.co, again a@b.co, other c@d.org",
            "id": "001A0000006Vm9rIAC",
            "word": "ABCDEFGHIJKLMNO",
            "n": 3,
            "none": None,
        }
    )
    assert out["api_key"] == "<redacted>"
    assert out["refresh_token"] == "<redacted>"
    assert out["nested"][0]["MobilePhone"].startswith("555-01")
    assert out["nested"][0]["Fax"] == ""
    assert out["note"] == "mail me: user1@example.com, again user1@example.com, other user2@example.com"
    assert out["id"] != "001A0000006Vm9rIAC"
    assert out["id"].startswith("001")
    assert out["word"] == "ABCDEFGHIJKLMNO"  # no digits → not an id
    assert out["n"] == 3
    assert out["none"] is None
    assert r.text("login.salesforce.com and foo.my.salesforce.com") == (
        "login.salesforce.com and example.my.salesforce.com"
    )
    assert r.text("x@example.com") == "x@example.com"
    assert gzip  # imported for the >64 KiB request path above
