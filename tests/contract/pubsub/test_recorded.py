"""``sf.pubsub.subscribe`` replaying responses recorded from a real org, for both clients.

Recorded 2026-11-12 from a Developer Edition org (API v68.0) with a raw gRPC probe; see
``tests/fixtures/recorded/pubsub`` and ``tests/sandbox/test_pubsub.py``. Evidence for V27, V28,
V75, V77 and V79 (big-bang.md §21):

- V27: a CDC update's ``changedFields`` / ``nulledFields`` bitmaps (top-level and compound
  ``{pos}-0x…`` entries; bit 0 = least-significant bit), including the nulled-compound-only
  form with no leading ``0x…`` entry.
- V28, V77: DateTime fields are a ``long`` of epoch milliseconds with ``doc``
  ``"CreatedDate:DateTime"``, ``"Data:DateTime"`` or ``"Data:DateTime:00N…"``; Date fields are a
  ``long`` of epoch milliseconds (midnight UTC) with ``doc`` ``"Data:DateOnly:00N…"``. No
  logical types. Salesforce's docs say only that Date and DateTime fields are epoch numbers
  (https://developer.salesforce.com/docs/platform/pub-sub-api/guide/event-deserialization-considerations.html,
  read from search excerpts: the page blocks non-browser clients); the ``doc`` markers are
  org-observed.
- V75: error status, ``error-code`` and ``rpc-id`` trailers.
- ``event_id``: change events carry a ``ConsumerEvent.event.id`` UUID too, stable on
  redelivery, although *Event Message Durability*
  (https://developer.salesforce.com/docs/platform/pub-sub-api/guide/event-message-durability.html)
  says the id is present on platform events only.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import grpc
import pytest
import respx

import even_simpler_salesforce as esf

from ...conftest import TOKEN_URL, token_body
from ._server import (
    RECORDED_ERRORS,
    batch,
    recorded,
    recorded_event,
    recorded_failure,
    rid,
    sends,
)
from .conftest import PubSubHarness

CDC = recorded("events_account_change_update.json")
DATE_EVENT = recorded("event_esf_sandbox_date_event.json")
CDC_TOPIC = "/data/AccountChangeEvent"
PE_TOPIC = "/event/ESF_Sandbox_Event__e"


def test_recorded_change_event_header_and_bitmaps(ps: PubSubHarness) -> None:
    first, second = CDC["events"]
    ps.server.script(sends(batch(recorded_event(first, 1), recorded_event(second, 2))))
    upd, nulled_city = ps.consume(CDC_TOPIC, n=2)

    assert upd.event_id == first["id"]  # set on change events too
    assert upd.schema_id == first["schema_id"]
    ch = upd.change
    assert ch is not None
    assert ch.entity_name == "Account"
    assert ch.change_type is esf.ChangeType.UPDATE
    assert ch.record_ids == ("001xx0000000001AAA",)
    # ["0x180000410200", "4-0x03"]: Website, Description, LastModifiedDate, Esf_Day__c and
    # Esf_When__c, plus Street and City of BillingAddress (field 4).
    assert ch.changed_fields == (
        "Website",
        "Description",
        "LastModifiedDate",
        "Esf_Day__c",
        "Esf_When__c",
        "BillingAddress.Street",
        "BillingAddress.City",
    )
    # ["0x010200", "4-0x01"]
    assert ch.nulled_fields == ("Website", "Description", "BillingAddress.Street")
    assert ch.diff_fields == ()
    assert ch.sequence_number == 1
    assert ch.commit_timestamp == datetime.fromtimestamp(1794437524, tz=timezone.utc)
    assert ch.change_origin == "com/salesforce/api/rest/68.0;client=esf-sandbox"

    assert upd.payload["Esf_Day__c"] == date(2026, 11, 12)
    assert upd.payload["Esf_When__c"] == datetime(2026, 11, 12, 10, 30, tzinfo=timezone.utc)
    assert isinstance(upd.payload["LastModifiedDate"], datetime)
    assert upd.payload["BillingAddress"]["City"] == "Paris"

    # Only a compound component was nulled: just a "{pos}-0x…" entry, no leading "0x…".
    assert nulled_city.change is not None
    assert nulled_city.change.nulled_fields == ("BillingAddress.City",)
    assert nulled_city.change.changed_fields == ("LastModifiedDate", "BillingAddress.City")
    assert nulled_city.event_id == second["id"]


def test_recorded_platform_event_dates(ps: PubSubHarness) -> None:
    ps.server.script(sends(batch(recorded_event(DATE_EVENT, 1))))
    (ev,) = ps.consume("/event/ESF_Sandbox_Date_Event__e", n=1)
    assert ev.event_id == DATE_EVENT["id"]
    assert ev.change is None
    assert ev.payload["Day__c"] == date(2026, 11, 12)
    assert ev.payload["When__c"] == datetime(2026, 11, 12, 10, 30, tzinfo=timezone.utc)
    assert ev.payload["CreatedDate"] == datetime.fromtimestamp(1794437345.334, tz=timezone.utc)
    assert ev.payload["Message__c"] == "esf-sandbox-0000aaaa-date"


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("subscribe_unknown_topic", esf.NotFound),
        ("subscribe_bad_topic_syntax", esf.Forbidden),
        ("subscribe_num_requested_0", esf.PubSubError),
    ],
)
def test_recorded_errors_are_typed(ps: PubSubHarness, name: str, cls: type[esf.SalesforceError]) -> None:
    err = RECORDED_ERRORS[name]
    ps.server.script(recorded_failure(name))
    with pytest.raises(cls) as ei:
        ps.consume(PE_TOPIC, n=1)
    assert type(ei.value) is cls
    assert ei.value.error_code == err["trailers"]["error-code"]
    assert ei.value.request_id == err["trailers"]["rpc-id"]
    assert ei.value.message == err["details"]
    assert len(ps.server.streams) == 1


@pytest.mark.parametrize("name", ["subscribe_bad_replay", "subscribe_old_replay"])
def test_recorded_rejected_replay_id(ps: PubSubHarness, name: str) -> None:
    ps.server.script(recorded_failure(name))
    with pytest.raises(esf.ReplayUnavailable) as ei:
        ps.consume(PE_TOPIC, n=1, replay=rid(3))
    assert ei.value.replay_id == rid(3)
    assert ei.value.request_id == RECORDED_ERRORS[name]["trailers"]["rpc-id"]


@pytest.mark.parametrize("name", ["subscribe_bad_token", "subscribe_no_tenant"])
def test_recorded_auth_errors_refresh_once(ps: PubSubHarness, mock: respx.MockRouter, name: str) -> None:
    route = mock.post(TOKEN_URL).respond(200, json=token_body("AT-refreshed-0002"))
    ps.server.script(recorded_failure(name), recorded_failure(name))
    with pytest.raises(esf.SessionExpired) as ei:
        ps.consume(PE_TOPIC, n=1)
    assert route.call_count == 1
    assert ei.value.error_code == RECORDED_ERRORS[name]["trailers"]["error-code"]


def test_recorded_trailer_keys() -> None:
    # Every recorded failure carries the code and the RPC id under these keys (V75).
    for err in RECORDED_ERRORS.values():
        assert err["trailers"]["error-code"].startswith("sfdc.platform.eventbus.grpc.")
        assert err["trailers"]["rpc-id"]
        assert err["trailers"]["rpc-id"] in err["details"]
    statuses: dict[str, Any] = {k: v["status"] for k, v in RECORDED_ERRORS.items()}
    assert statuses["subscribe_unknown_topic"] == grpc.StatusCode.NOT_FOUND.name
    assert statuses["subscribe_bad_token"] == grpc.StatusCode.UNAUTHENTICATED.name
    assert statuses["get_schema_unknown"] == grpc.StatusCode.UNAVAILABLE.name
