"""``client_name=`` → ``Sforce-Call-Options: client=<name>`` on API calls (simple-salesforce #726)."""

from __future__ import annotations

import pytest
import respx

from ..conftest import DATA, Harness


def test_client_name_header(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/limits").respond(json={})
    sf = h.client(client_name="acme-sync:v2")
    h.run(sf.limits())
    assert route.calls.last.request.headers["Sforce-Call-Options"] == "client=acme-sync:v2"


def test_no_header_by_default(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/limits").respond(json={})
    h.run(h.client().limits())
    assert "Sforce-Call-Options" not in route.calls.last.request.headers


@pytest.mark.parametrize("bad", ["", "has space", "a;b", "x" * 65, "a=b"])
def test_client_name_validated(h: Harness, bad: str) -> None:
    with pytest.raises(ValueError, match="client_name"):
        h.client(client_name=bad)


@pytest.mark.parametrize("method", ["GET", "get", "HEAD"])
def test_request_get_never_sends_a_body(h: Harness, method: str) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="body"):
        h.run(sf.request(method, "x", json={"a": 1}))
    with pytest.raises(ValueError, match="body"):
        h.run(sf.request(method, "x", content=b"a"))


def test_collections_event_counts_failed_records(h: Harness, mock: respx.MockRouter) -> None:
    """Partial failures show up in logs even if raise_for_errors() is never called (D28, #817)."""
    import even_simpler_salesforce as esf

    from ..conftest import Recorder

    mock.post(f"{DATA}/composite/sobjects").respond(
        json=[
            {"id": "001000000000001AAA", "success": True, "errors": []},
            {
                "success": False,
                "errors": [{"statusCode": "REQUIRED_FIELD_MISSING", "message": "x", "fields": []}],
            },
        ]
    )
    rec = Recorder()
    sf = h.client(recorder=rec)
    with esf.capture() as summary:
        res = h.run(sf.collections.create([{"Name": "a"}, {}], sobject="Account"))
    assert len(res.failed) == 1
    assert rec.requests[-1]["records_failed"] == 1
    assert summary.as_fields()["sf_records_failed"] == 1
