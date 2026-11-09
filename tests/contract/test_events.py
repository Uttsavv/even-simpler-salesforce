"""``sf.request`` and friends (§11): fields, and what must never appear in any event."""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf
from even_simpler_salesforce.observability import DebugPrinter

from ..conftest import DATA, INSTANCE, TOKEN_URL, Harness, Recorder, make_token, token_body, web_auth

LEAD_ID = "00Qxx0000000001AAA"
SECRET_EMAIL = "very.private@example.com"
SECRET_NAME = "Zebulon-Secret-Lastname"
SPEC_FIELDS = {
    "operation",
    "sobject",
    "method",
    "path_template",
    "status",
    "outcome",
    "error_code",
    "error_class",
    "duration_ms",
    "attempts",
    "retry_reasons",
    "refreshed",
    "records",
    "bytes_in",
    "bytes_out",
    "api_used",
    "api_max",
    "caller",
    "priority",
    "soql_hash",
    "request_id",
    "api_version",
    "client_id_hash",
}


def everything(rec: Recorder) -> str:
    return "\n".join(f"{e.name} {e.fields!r}" for e in rec.events)


def test_request_event_fields(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{DATA}/query").respond(
        json={"totalSize": 2, "done": True, "records": [{"Id": "1"}, {"Id": "2"}]},
        headers={"Sforce-Limit-Info": "api-usage=12000/100000", "Sforce-Request-Id": "req-123"},
    )
    sf = h.client(recorder=rec)
    soql = esf.soql("SELECT Id FROM Lead WHERE Email = {}", SECRET_EMAIL)
    h.run(sf.query(soql))
    (ev,) = rec.requests
    assert set(ev.fields) >= SPEC_FIELDS
    assert "soql" not in ev.fields  # only with log_soql="redacted"
    f = ev.fields
    assert f["operation"] == "query"
    assert f["sobject"] == "Lead"
    assert f["method"] == "GET"
    assert f["path_template"] == "/query"
    assert f["status"] == 200
    assert f["outcome"] == "ok"
    assert f["error_code"] is None
    assert f["error_class"] is None
    assert f["attempts"] == 1
    assert f["retry_reasons"] == []
    assert f["refreshed"] is False
    assert f["records"] == 2
    assert f["bytes_in"] > 0
    assert f["bytes_out"] == 0
    assert (f["api_used"], f["api_max"]) == (12000, 100000)
    assert f["caller"] is None
    assert f["priority"] == "normal"
    assert f["soql_hash"] == esf.core.soql.soql_hash(soql)
    assert f["request_id"] == "req-123"
    assert f["api_version"] == "68.0"
    assert f["client_id_hash"] == sf._runtime.auth.client_id_hash
    assert f["duration_ms"] >= 0
    assert ev.timestamp.tzinfo is not None
    assert SECRET_EMAIL not in everything(rec)


def test_log_soql_redacted(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{DATA}/query").respond(json={"totalSize": 0, "done": True, "records": []})
    sf = h.client(recorder=rec, log_soql="redacted")
    h.run(sf.query(esf.soql("SELECT Id FROM Lead WHERE Email = {} LIMIT {}", SECRET_EMAIL, 5)))
    assert rec.requests[0]["soql"] == "SELECT Id FROM Lead WHERE Email = ? LIMIT ?"
    assert SECRET_EMAIL not in everything(rec)


def test_soql_literals_not_leaked_through_error_messages(
    h: Harness, mock: respx.MockRouter, rec: Recorder
) -> None:
    soql = esf.soql("SELECT Nope FROM Lead WHERE Email = {}", SECRET_EMAIL)
    # Salesforce echoes the query text in MALFORMED_QUERY / INVALID_FIELD messages.
    message = f"\n{soql}\n       ^\nERROR at Row:1:Column:8\nNo such column 'Nope' on entity 'Lead'."
    mock.get(f"{DATA}/query").respond(400, json=[{"errorCode": "INVALID_FIELD", "message": message}])
    buf = io.StringIO()
    sf = h.client(recorder=rec, observers=[DebugPrinter(1, stream=buf)])
    with pytest.raises(esf.InvalidField) as ei:
        h.run(sf.query(soql))
    assert SECRET_EMAIL in ei.value.message  # the exception keeps it for the developer
    assert SECRET_EMAIL not in everything(rec)
    assert SECRET_EMAIL not in buf.getvalue()
    assert rec.requests[0]["error_code"] == "INVALID_FIELD"
    assert "No such column" in rec.requests[0]["error_message"]


def test_error_event_fields(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.post(f"{DATA}/sobjects/Lead/").respond(
        400,
        json=[{"errorCode": "REQUIRED_FIELD_MISSING", "message": "Required fields are missing: [Company]"}],
        headers={"Sforce-Request-Id": "req-err"},
    )
    sf = h.client(recorder=rec)
    with pytest.raises(esf.RequiredFieldMissing):
        h.run(sf.sobject("Lead").create({"LastName": SECRET_NAME}))
    (ev,) = rec.requests
    assert ev["outcome"] == "error"
    assert ev["error_code"] == "REQUIRED_FIELD_MISSING"
    assert ev["error_class"] == "RequiredFieldMissing"
    assert ev["status"] == 400
    assert ev["request_id"] == "req-err"
    assert ev["records"] == 1
    assert ev["bytes_out"] > 0
    assert SECRET_NAME not in everything(rec)  # record bodies are never logged
    assert sf.usage.snapshot().errors_by_code == {"REQUIRED_FIELD_MISSING": 1}


def test_no_secrets_in_any_event(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    access_old, access_new = "00Dxx!AQ-old-access-token-value", "00Dxx!AQ-new-access-token-value"
    refresh_old, refresh_new = "5Aep-old-refresh-token-value", "5Aep-new-refresh-token-value"

    def api(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == f"Bearer {access_old}":
            return httpx.Response(
                401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}]
            )
        return httpx.Response(200, json={"totalSize": 0, "done": True, "records": []})

    mock.get(f"{DATA}/query").mock(side_effect=api)
    mock.post(f"{DATA}/sobjects/Lead/").respond(201, json={"id": LEAD_ID})
    mock.post(TOKEN_URL).respond(json=token_body(access_new, refresh_new))
    auth = web_auth()
    sf = h.client(
        auth,
        token=make_token(access_old, refresh_old),
        recorder=rec,
        log_soql="redacted",
        observers=[DebugPrinter(2, stream=io.StringIO())],
    )
    sf._transport.debug = 2  # also emit per-attempt debug events with headers
    h.run(sf.query(esf.soql("SELECT Id FROM Lead WHERE Email = {}", SECRET_EMAIL)))
    h.run(sf.sobject("Lead").create({"LastName": SECRET_NAME}))
    text = everything(rec)
    names = {e.name for e in rec.events}
    assert {"sf.request", "sf.auth.refreshed", "sf.debug.attempt"} <= names
    for secret in (
        access_old,
        access_new,
        refresh_old,
        refresh_new,
        "web-s3cr3t",
        "Bearer",
        SECRET_EMAIL,
        SECRET_NAME,
        auth.token_key,
    ):
        assert secret not in text, secret
    debug = rec.named("sf.debug.attempt")[0]
    assert debug["request_headers"]["Authorization"] == "<redacted>"
    assert "?" not in debug["url"]


def test_retry_event_fields(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{DATA}/limits")
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={})]
    sf = h.client(recorder=rec)
    h.run(sf.limits())
    (retry,) = rec.named("sf.retry")
    assert retry.fields == {
        "operation": "limits",
        "attempt": 1,
        "reason": "http_503",
        "delay_ms": 250.0,
        "error_code": None,
        "status": 503,
    }
    assert rec.requests[0]["retry_reasons"] == ["http_503"]
    assert rec.requests[0]["attempts"] == 2


def test_threshold_events_from_headers(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{DATA}/limits")
    route.side_effect = [
        httpx.Response(200, json={}, headers={"Sforce-Limit-Info": "api-usage=40/100"}),
        httpx.Response(200, json={}, headers={"Sforce-Limit-Info": "api-usage=85/100; api-bursts=3/750"}),
        httpx.Response(200, json={}, headers={"Sforce-Limit-Info": "api-usage=86/100"}),
    ]
    sf = h.client(recorder=rec, budget=esf.Budget(pause_low_priority_above=None))
    for _ in range(3):
        h.run(sf.limits())
    th = rec.named("sf.usage.threshold")
    assert [e["threshold"] for e in th] == [0.5, 0.8]
    assert th[1]["used"] == 85
    snap = sf.usage.snapshot()
    assert (snap.api_used, snap.api_max, snap.api_bursts_used, snap.api_bursts_max) == (86, 100, None, None)
    assert rec.requests[-1]["api_used"] == 86


def test_observer_exceptions_do_not_break_calls(h: Harness, mock: respx.MockRouter) -> None:
    class Boom:
        def on_event(self, event: Any) -> None:
            raise RuntimeError("bad observer")

    mock.get(f"{DATA}/limits").respond(json={})
    sf = h.client(observers=[Boom()])
    h.run(sf.limits())
    assert sf.observers.errors == 1


def test_esf_debug_env_installs_printer(
    h: Harness, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ESF_DEBUG", "1")
    mock.get(f"{DATA}/limits").respond(json={})
    sf = h.client()
    assert any(isinstance(o, DebugPrinter) for o in sf.observers.observers)
    h.run(sf.limits())
    err = capsys.readouterr().err
    assert err.split()[:3] == ["esf", "GET", "limits"]
    monkeypatch.delenv("ESF_DEBUG")
    assert not any(isinstance(o, DebugPrinter) for o in h.client().observers.observers)


def test_static_token_has_no_client_id_hash(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{DATA}/limits").respond(json={})
    sf = h.client(esf.StaticToken("00D!x", INSTANCE), recorder=rec)
    h.run(sf.limits())
    assert rec.requests[0]["client_id_hash"] is None
