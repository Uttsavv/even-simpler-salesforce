"""Platform event publishing (§9.10), for both clients.

Responses in ``tests/fixtures/recorded/platform_events`` are recorded from a Developer Edition
org (API v68.0, 2026-11-12, V36 in big-bang.md §21): ``publish*.json`` and ``publish_many*.json``.
``publish_limit.json`` and ``publish_unavailable.json`` stay **synthetic**: a test org can't
produce them on demand. Sources:

- Publish with Salesforce APIs (``POST sobjects/{Event__e}``; a queued event returns
  ``OPERATION_ENQUEUED`` with the event UUID in ``errors``):
  https://developer.salesforce.com/docs/atlas.en-us.platform_events.meta/platform_events/platform_events_publish_api.htm
- EventUuid: https://developer.salesforce.com/docs/atlas.en-us.platform_events.meta/platform_events/platform_events_event_uuid.htm
- Error status codes (``LIMIT_EXCEEDED``, ``PLATFORM_EVENT_PUBLISHING_UNAVAILABLE``):
  https://developer.salesforce.com/docs/atlas.en-us.platform_events.meta/platform_events/platform_events_error_status_codes.htm
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture, load_response

EVENT = "ESF_Sandbox_Event__e"
UUID = "6dc846c4-d286-4dd7-b8a8-6266d1e79674"  # EventUuid in the recorded publish.json


def test_publish_returns_uuid(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Recorded: HTTP 201, ``success: true`` and one ``OPERATION_ENQUEUED`` entry (the UUID)."""
    status, body = load_response("platform_events/publish.json")
    assert status == 201
    route = mock.post(f"{DATA}/sobjects/{EVENT}").respond(status, json=body)
    sf = h.client(recorder=rec)
    res = h.run(sf.platform_events.publish(EVENT, {"Message__c": "esf-sandbox probe"}))
    assert res == esf.PublishResult(ok=True, id="e00000000000001AAA", uuid=UUID)
    assert json.loads(route.calls[0].request.content) == {"Message__c": "esf-sandbox probe"}
    assert rec.requests[0]["operation"] == "platform_events.publish"
    assert rec.requests[0]["sobject"] == EVENT


def test_publish_limit_exceeded_is_event_publish_failed(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``platform_events/publish_limit.json``."""
    route = mock.post(f"{DATA}/sobjects/{EVENT}").respond(
        400, json=load_fixture("platform_events/publish_limit.json")
    )
    sf = h.client()
    with pytest.raises(esf.EventPublishFailed) as ei:
        h.run(sf.platform_events.publish(EVENT, {}))
    assert type(ei.value) is esf.EventPublishFailed
    assert isinstance(ei.value, esf.RecordError)
    assert ei.value.error_code == "LIMIT_EXCEEDED"
    assert ei.value.status == 400
    assert ei.value.operation == "platform_events.publish"
    assert route.call_count == 1


def test_publish_unavailable_is_retryable_class(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic fixture ``platform_events/publish_unavailable.json``; a publish is never resent."""
    route = mock.post(f"{DATA}/sobjects/{EVENT}").respond(
        503, json=load_fixture("platform_events/publish_unavailable.json")
    )
    sf = h.client()
    with pytest.raises(esf.EventPublishUnavailable) as ei:
        h.run(sf.platform_events.publish(EVENT, {}))
    assert ei.value.retryable is True
    assert route.call_count == 1


def test_publish_known_record_error_keeps_class(h: Harness, mock: respx.MockRouter) -> None:
    """Synthetic body: a code with its own class keeps it."""
    mock.post(f"{DATA}/sobjects/{EVENT}").respond(
        400,
        json=[{"errorCode": "REQUIRED_FIELD_MISSING", "message": "missing", "fields": ["Order_Number__c"]}],
    )
    sf = h.client()
    with pytest.raises(esf.RequiredFieldMissing):
        h.run(sf.platform_events.publish(EVENT, {}))


@pytest.mark.parametrize(
    ("fixture", "cls", "code"),
    [
        ("publish_bad_field.json", esf.InvalidField, "INVALID_FIELD"),
        ("publish_too_long.json", esf.StringTooLong, "STRING_TOO_LONG"),
    ],
)
def test_publish_recorded_field_errors(
    h: Harness, mock: respx.MockRouter, fixture: str, cls: type[esf.SalesforceError], code: str
) -> None:
    """Recorded: an unknown field and a too-long value are HTTP 400 REST error lists."""
    status, body = load_response(f"platform_events/{fixture}")
    assert status == 400
    mock.post(f"{DATA}/sobjects/{EVENT}").respond(status, json=body)
    sf = h.client()
    with pytest.raises(cls) as ei:
        h.run(sf.platform_events.publish(EVENT, {"Message__c": "x"}))
    assert type(ei.value) is cls
    assert ei.value.error_code == code
    assert ei.value.status == 400


def test_publish_success_false_in_2xx_body_raises(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/sobjects/{EVENT}").respond(
        201,
        json={
            "success": False,
            "errors": [{"statusCode": "LIMIT_EXCEEDED", "message": "limit", "fields": []}],
        },
    )
    sf = h.client()
    with pytest.raises(esf.EventPublishFailed):
        h.run(sf.platform_events.publish(EVENT, {}))


def test_publish_success_false_without_details(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/sobjects/{EVENT}").respond(201, json={"success": False, "errors": []})
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="without error details"):
        h.run(sf.platform_events.publish(EVENT, {}))


def test_publish_rejects_names_without_event_suffix(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="__e"):
        h.run(sf.platform_events.publish("Account", {}))
    with pytest.raises(ValueError, match="__e"):
        h.run(sf.platform_events.publish_many("Account", [{}]))


def test_publish_many_goes_through_collections(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """Recorded ``publish_many_bad_field.json``: HTTP 200, one event queued, one unknown field."""
    status, body = load_response("platform_events/publish_many_bad_field.json")
    assert status == 200
    route = mock.post(f"{DATA}/composite/sobjects").respond(status, json=body)
    sf = h.client(recorder=rec)
    batch = h.run(
        sf.platform_events.publish_many(EVENT, [{"Message__c": "esf-sandbox 1"}, {"Bogus__c": "y"}])
    )
    assert [r.ok for r in batch] == [True, False]
    assert batch[0].uuid == "24f526db-6823-40ce-99dd-b09372b09b1b"
    assert batch[0].errors == ()
    assert batch[1].uuid is None
    assert batch[1].errors[0].error_code == "INVALID_FIELD"
    assert batch.sobject == EVENT
    body = json.loads(route.calls[0].request.content)
    assert body["records"][0] == {"attributes": {"type": EVENT}, "Message__c": "esf-sandbox 1"}
    assert rec.requests[0]["records_failed"] == 1
    with pytest.raises(esf.PartialFailure):
        batch.raise_for_errors()


def test_publish_many_all_queued(h: Harness, mock: respx.MockRouter) -> None:
    """Recorded ``publish_many.json``: each row has its own ``OPERATION_ENQUEUED`` UUID."""
    status, body = load_response("platform_events/publish_many.json")
    mock.post(f"{DATA}/composite/sobjects").respond(status, json=body)
    sf = h.client()
    batch = h.run(sf.platform_events.publish_many(EVENT, [{"Message__c": "1"}, {"Message__c": "2"}]))
    assert [(r.ok, r.uuid) for r in batch] == [
        (True, "bed40404-2b79-4e50-8587-4c4daf9c75d6"),
        (True, "82511e3d-0bf3-4b10-ab3f-2a5ba4510cfd"),
    ]
    assert not batch.failed


def test_publish_many_partial_chunk_failure_converts_results(h: Harness, mock: respx.MockRouter) -> None:
    """The second chunk (events 200-249) fails with a 500. The sync client sends chunks
    concurrently, so the failure is routed by the chunk's contents, not by call order."""

    def second_chunk_fails(request: httpx.Request) -> httpx.Response:
        records = json.loads(request.content)["records"]
        if records[0]["N__c"] == 200:
            return httpx.Response(500, json=[{"errorCode": "UNKNOWN_EXCEPTION", "message": "boom"}])
        return httpx.Response(200, json=[load_fixture("platform_events/publish.json")] * len(records))

    route = mock.post(f"{DATA}/composite/sobjects").mock(side_effect=second_chunk_fails)
    sf = h.client()
    with pytest.raises(esf.PartialFailure) as ei:
        h.run(
            sf.platform_events.publish_many(
                EVENT,
                [{"N__c": i} for i in range(250)],
            )
        )
    results = ei.value.results
    assert results is not None
    assert all(isinstance(r, esf.PublishResult) for r in results)
    assert len(results) == 250
    assert [r.ok for r in results] == [True] * 200 + [False] * 50
    assert results.sobject == EVENT
    firsts = sorted(json.loads(c.request.content)["records"][0]["N__c"] for c in route.calls)
    assert firsts == [0, 200]  # one request per chunk; the failed chunk was not resent


def test_publish_many_empty(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    assert h.run(sf.platform_events.publish_many(EVENT, [])).results == []


def test_publish_many_partial_failure_without_results(
    h: Harness, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    sf = h.client()

    def boom(*_: object, **__: object) -> None:
        raise esf.PartialFailure("x", results=None)

    async def aboom(*a: object, **k: object) -> None:
        boom()

    monkeypatch.setattr(sf.collections, "create", aboom if h.is_async else boom)
    with pytest.raises(esf.PartialFailure) as ei:
        h.run(sf.platform_events.publish_many(EVENT, [{}]))
    assert ei.value.results is None


def test_publish_many_request_failure_is_mapped(h: Harness, mock: respx.MockRouter) -> None:
    """When the only collections request fails as a whole, the error gets its EventBus class
    (and that class's docs entry), as with ``publish()``."""
    mock.post(f"{DATA}/composite/sobjects").respond(
        503, json=load_fixture("platform_events/publish_unavailable.json")
    )
    sf = h.client()
    with pytest.raises(esf.EventPublishUnavailable) as ei:
        h.run(sf.platform_events.publish_many(EVENT, [{"Order_Number__c": "1"}]))
    assert ei.value.retryable is True
    assert ei.value.docs_url is not None
    assert ei.value.docs_url.endswith("#eventpublishunavailable")
