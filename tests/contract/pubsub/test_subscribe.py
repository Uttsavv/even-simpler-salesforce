"""``sf.pubsub.subscribe`` against an in-process gRPC server, for both clients (§9.16, D31).

The responses here are **synthetic** (see ``_server.py``), shaped like the recordings in
``tests/fixtures/recorded/pubsub``; ``test_recorded.py`` replays the recordings themselves. Sources,
read 2026-11-09 (ADR 0013):

- Subscribe RPC and flow control:
  https://developer.salesforce.com/docs/platform/pub-sub-api/references/methods/subscribe-rpc.html
  https://developer.salesforce.com/docs/platform/pub-sub-api/guide/flow-control.html
- Error codes and retries:
  https://developer.salesforce.com/docs/platform/pub-sub-api/guide/handling-errors.html
  https://developer.salesforce.com/docs/platform/pub-sub-api/guide/retry-rpc-calls.html
- Bitmaps: https://developer.salesforce.com/docs/platform/pub-sub-api/guide/event-deserialization-considerations.html
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import grpc
import pytest
import respx

import even_simpler_salesforce as esf

from ...conftest import INSTANCE, ORG_ID, TOKEN_URL, Recorder, make_token, token_body
from ._server import (
    ACCOUNT_SCHEMA_ID,
    ORDER_SCHEMA_ID,
    account_change,
    batch,
    ends,
    fails,
    keepalive,
    order,
    rid,
    sends,
)
from .conftest import PubSubHarness

TOPIC = "/event/Order_Event__e"
KEY = f"{ORG_ID}:{TOPIC}"


# --- connection, decoding, events --------------------------------------------------------------


def test_receives_decoded_platform_events(ps: PubSubHarness, rec: Recorder) -> None:
    ps.server.script(sends(batch(order(1), order(2))))
    evs = ps.consume(n=2)

    assert [e.event_id for e in evs] == ["uuid-1", "uuid-2"]
    first = evs[0]
    assert first.topic == TOPIC
    assert first.replay_id == rid(1)
    assert first.schema_id == ORDER_SCHEMA_ID
    assert first.change is None
    assert first.payload["Order_Number__c"] == "O-1"
    assert first.payload["Quantity__c"] == 1  # a plain long stays an int
    assert first.payload["CreatedDate"] == datetime(2026, 1, 1, tzinfo=timezone.utc)

    stream = ps.server.streams[0]
    assert stream.metadata["accesstoken"] == "AT-initial-0001"
    assert stream.metadata["instanceurl"] == INSTANCE
    assert stream.metadata["tenantid"] == ORG_ID
    assert stream.first.topic_name == TOPIC
    assert stream.first.num_requested == 100
    assert stream.first.replay_preset == 0  # LATEST
    # One GetSchema call for two events with the same schema.
    assert [s for s, _ in ps.server.schema_calls] == [ORDER_SCHEMA_ID]
    assert ps.server.schema_calls[0][1]["accesstoken"] == "AT-initial-0001"

    (sub,) = rec.named("sf.pubsub.subscribed")
    assert sub["topic"] == TOPIC
    assert sub["replay_preset"] == "LATEST"
    assert sub["resumed_from"] == "none"
    assert sub["stream_no"] == 1
    (b,) = rec.named("sf.pubsub.batch")
    assert b["events"] == 2
    assert b["lag_ms"] is not None
    (closed,) = rec.named("sf.pubsub.closed")
    assert closed["outcome"] == "ok"
    assert closed["events"] == 2
    assert closed["reconnects"] == 0
    assert rec.requests == []  # streams emit no sf.request
    # Payload values are never logged.
    assert "O-1" not in repr([e.fields for e in rec.events])


def test_schema_cache_is_shared_by_subscriptions(ps: PubSubHarness) -> None:
    ps.server.script(sends(batch(order(1))), sends(batch(order(2))))
    ps.consume(n=1)
    ps.consume(n=1)
    assert len(ps.server.schema_calls) == 1
    assert len(ps.server.streams) == 2


def test_change_event_header_and_bitmaps(ps: PubSubHarness) -> None:
    ps.server.script(
        sends(
            batch(
                account_change(
                    1,
                    changed=["0x12", "3-0x02"],
                    nulled=["3-0x06"],
                    diff=["0x04"],
                    values={
                        "BillingAddress": {
                            "City": "Paris",
                            "Street": None,
                            "State": None,
                            "PostalCode": None,
                            "Country": None,
                        }
                    },
                )
            )
        )
    )
    (ev,) = ps.consume("/data/AccountChangeEvent", n=1)
    ch = ev.change
    assert ch is not None
    assert ev.event_id == "cdc-uuid-1"  # passed through: Salesforce sets it on change events too
    assert ev.schema_id == ACCOUNT_SCHEMA_ID
    assert ch.entity_name == "Account"
    assert ch.change_type is esf.ChangeType.UPDATE
    assert not ch.is_gap
    assert not ch.is_overflow
    assert ch.record_ids == ("001xx0000000001AAA",)
    assert ch.changed_fields == ("Name", "Phone", "BillingAddress.City")
    assert ch.nulled_fields == ("BillingAddress.City", "BillingAddress.State")
    assert ch.diff_fields == ("Description",)
    assert ch.transaction_key == "tx-1"
    assert ch.sequence_number == 1
    assert ch.commit_timestamp == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ch.commit_number == 1001
    assert ev.payload["LastModifiedDate"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ev.payload["BillingAddress"]["City"] == "Paris"


def test_gap_and_overflow_events(ps: PubSubHarness) -> None:
    ps.server.script(
        sends(
            batch(
                account_change(
                    1, "GAP_UPDATE", changed=[], record_ids=["001xx0000000001AAA", "001xx0000000002AAA"]
                ),
                account_change(2, "GAP_OVERFLOW", changed=[], record_ids=["000000000000000AAA"]),
            )
        )
    )
    gap, overflow = ps.consume("/data/AccountChangeEvent", n=2)
    assert gap.change.is_gap
    assert not gap.change.is_overflow
    assert gap.change.record_ids == ("001xx0000000001AAA", "001xx0000000002AAA")
    assert overflow.change.is_gap
    assert overflow.change.is_overflow
    assert overflow.change.record_ids == ()


def test_undecodable_payload_raises_unexpected_response(ps: PubSubHarness, rec: Recorder) -> None:
    bad = order(7)
    bad.event.payload = b"\xff\xff\xff"
    ps.server.script(sends(batch(bad)))
    with pytest.raises(esf.UnexpectedResponse) as ei:
        ps.consume(n=1)
    assert ei.value.event_id == "uuid-7"
    assert ei.value.replay_id == rid(7)
    assert ei.value.path == TOPIC
    assert rec.named("sf.pubsub.closed")[0]["outcome"] == "error"


def test_empty_event_id_is_none(ps: PubSubHarness) -> None:
    ev = order(1)
    ev.event.id = ""
    ps.server.script(sends(batch(ev)))
    (got,) = ps.consume(n=1)
    assert got.event_id is None


def test_unavailable_schema_reconnects_then_raises(ps: PubSubHarness) -> None:
    # Salesforce answers GetSchema for an unknown id with UNAVAILABLE / schema.api.unavailable
    # (recorded), so the client treats it as transient and resubscribes until attempts run out.
    ps.server.schemas.clear()
    ps.server.script(*[sends(batch(order(1)))] * 3)
    with pytest.raises(esf.PubSubError) as ei:
        ps.consume(n=1)
    assert type(ei.value) is esf.PubSubError
    assert ei.value.error_code == "sfdc.platform.eventbus.grpc.schema.api.unavailable"
    assert ei.value.request_id
    assert len(ps.server.schema_calls) == 3


def test_static_token_without_org_id_calls_userinfo_once(ps: PubSubHarness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{INSTANCE}/services/oauth2/userinfo").respond(200, json={"organization_id": ORG_ID})
    sf = ps.client(auth=esf.StaticToken(access_token="AT-static-1", instance_url=INSTANCE))
    ps.server.script(sends(batch(order(1))), sends(batch(order(2))))
    ps.consume(n=1, sf=sf)
    ps.consume(n=1, sf=sf)
    assert route.call_count == 1
    assert ps.server.streams[0].metadata["tenantid"] == ORG_ID
    assert ps.server.streams[0].metadata["accesstoken"] == "AT-static-1"


def test_userinfo_without_org_id_is_unexpected(ps: PubSubHarness, mock: respx.MockRouter) -> None:
    mock.get(f"{INSTANCE}/services/oauth2/userinfo").respond(200, json={})
    sf = ps.client(auth=esf.StaticToken(access_token="AT-static-1", instance_url=INSTANCE))
    with pytest.raises(esf.UnexpectedResponse):
        ps.consume(n=1, sf=sf)


def test_subscription_opens_once_and_reprs(ps: PubSubHarness) -> None:
    sub = ps.sf.pubsub.subscribe(TOPIC)
    assert "new" in repr(sub)
    ps.consume(n=0)
    if ps.h.is_async:

        async def twice() -> None:
            async with sub:
                pass
            async with sub:
                pass

        with pytest.raises(RuntimeError):
            ps.h.run(twice())
    else:
        with sub:
            pass
        with pytest.raises(RuntimeError), sub:
            pass


def test_iterating_a_closed_subscription_stops(ps: PubSubHarness) -> None:
    ps.server.script(sends(batch(order(1))))
    sub = ps.sf.pubsub.subscribe(TOPIC)
    if ps.h.is_async:

        async def go() -> list[Any]:
            async with sub as events:
                pass
            return [e async for e in events]

        assert ps.h.run(go()) == []
    else:
        with sub as events:
            pass
        assert list(events) == []


# --- flow control ----------------------------------------------------------------------------


def test_never_more_than_batch_size_outstanding(ps: PubSubHarness) -> None:
    ps.server.script(
        sends(
            batch(order(1), order(2)),
            batch(order(3), order(4)),
            batch(order(5)),
            batch(order(6), order(7), order(8)),
        )
    )
    evs = ps.consume(n=8, batch_size=4)
    assert [e.replay_id for e in evs] == [rid(i) for i in range(1, 9)]
    stream = ps.server.streams[0]
    assert stream.first.num_requested == 4
    assert stream.max_outstanding <= 4
    # Later requests never set a position.
    assert all(r.topic_name == "" and r.replay_id == b"" for r in stream.requests[1:])


def test_keepalives_are_not_yielded(ps: PubSubHarness) -> None:
    ps.server.script(sends(keepalive(rid(10)), batch(order(11)), keepalive(rid(12)), batch(order(13))))
    evs = ps.consume(n=2)
    assert [e.replay_id for e in evs] == [rid(11), rid(13)]


def test_batch_size_is_checked() -> None:
    sf = esf.Salesforce(esf.StaticToken(access_token="x", instance_url=INSTANCE))
    with pytest.raises(ValueError, match="batch_size"):
        sf.pubsub.subscribe(TOPIC, batch_size=101)


# --- replay store ---------------------------------------------------------------------------


def test_saves_after_each_batch_and_on_exit(ps: PubSubHarness) -> None:
    store = ps.store()
    seen: list[Any] = []
    ps.server.script(sends(batch(order(1), order(2)), batch(order(3), order(4))))

    def each(ev: Any) -> None:
        seen.append(store._data.get(KEY))

    ps.consume(n=3, replay_store=store, each=each)
    # While event i is handled, only fully consumed batches are saved.
    assert seen == [None, None, rid(2)]
    # A normal exit (here: break) counts the current event as processed.
    assert ps.load(store, KEY) == rid(3)


def test_no_save_when_the_block_raises(ps: PubSubHarness) -> None:
    store = ps.store()
    ps.server.script(sends(batch(order(1)), batch(order(2))))

    def each(ev: Any) -> None:
        if ev.replay_id == rid(2):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        ps.consume(n=5, replay_store=store, each=each)
    assert ps.load(store, KEY) == rid(1)
    assert ps.rec.named("sf.pubsub.closed")[0]["error_class"] == "RuntimeError"


def test_starts_from_the_stored_replay_id(ps: PubSubHarness, rec: Recorder) -> None:
    store = ps.store()
    ps.save(store, KEY, rid(41))
    ps.server.script(sends(batch(order(42))))
    ps.consume(n=1, replay_store=store, replay="earliest")
    first = ps.server.streams[0].first
    assert first.replay_preset == 2  # CUSTOM
    assert first.replay_id == rid(41)
    assert rec.named("sf.pubsub.subscribed")[0]["resumed_from"] == "store"
    assert rec.named("sf.pubsub.subscribed")[0]["replay_id"] == rid(41).hex()


def test_replay_argument_applies_without_a_stored_position(ps: PubSubHarness) -> None:
    ps.server.script(sends(batch(order(1))), sends(batch(order(2))))
    ps.consume(n=1, replay="earliest")
    ps.consume(n=1, replay=rid(5))
    assert ps.server.streams[0].first.replay_preset == 1  # EARLIEST
    assert ps.server.streams[1].first.replay_preset == 2
    assert ps.server.streams[1].first.replay_id == rid(5)


def test_keepalive_position_saved_only_when_drained(ps: PubSubHarness) -> None:
    store = ps.store()
    ps.server.script(sends(batch(order(1)), keepalive(rid(9))))
    ps.consume(n=1, replay_store=store)
    # Event 1 was the end of its batch: saved when the consumer asked for more; leaving the
    # block right after it doesn't wait for the keepalive.
    assert ps.load(store, KEY) == rid(1)

    store2 = ps.store()
    ps.server.script(sends(batch(order(1)), keepalive(rid(9)), batch(order(10))))
    ps.consume(n=2, replay_store=store2, each=lambda ev: None)
    assert ps.load(store2, KEY) == rid(10)


def test_keepalive_moves_the_saved_position(ps: PubSubHarness) -> None:
    store = ps.store()
    seen: list[Any] = []
    ps.server.script(sends(batch(order(1)), keepalive(rid(9)), batch(order(10))))
    ps.consume(n=2, replay_store=store, each=lambda ev: seen.append(store._data.get(KEY)))
    # Before event 10 is yielded the keepalive (nothing buffered) moved the position to 9.
    assert seen == [None, rid(9)]


# --- reconnects ------------------------------------------------------------------------------


def test_reconnect_resumes_from_the_last_processed_event(ps: PubSubHarness, rec: Recorder) -> None:
    ps.server.script(
        sends(batch(order(1), order(2)), then=lambda s: s.abort(grpc.StatusCode.UNAVAILABLE, "gone")),
        sends(batch(order(3))),
    )
    evs = ps.consume(n=3)
    assert [e.replay_id for e in evs] == [rid(1), rid(2), rid(3)]
    second = ps.server.streams[1].first
    assert second.replay_preset == 2
    assert second.replay_id == rid(2)
    (rc,) = rec.named("sf.pubsub.reconnect")
    assert rc["reason"] == "error"
    assert rc["error_code"] == "UNAVAILABLE"
    assert rc["rpc_id"] == "rpc-fail-1"
    assert rc["attempt"] == 1
    assert ps.sf.sleeps == [0.25]
    subs = rec.named("sf.pubsub.subscribed")
    assert [s["stream_no"] for s in subs] == [1, 2]
    assert subs[1]["resumed_from"] == "memory"
    assert rec.named("sf.pubsub.closed")[0]["reconnects"] == 1


def test_buffered_events_are_yielded_before_a_stream_failure(ps: PubSubHarness) -> None:
    ps.server.script(
        sends(
            batch(order(1), order(2), order(3)),
            then=lambda s: s.abort(grpc.StatusCode.INTERNAL, "RST_STREAM"),
        ),
    )

    # The stream fails while the consumer is still on event 1; events 2 and 3 are buffered and
    # still yielded: the failure is only seen when the buffer is empty and the client reads.
    evs = ps.consume(n=3)
    assert [e.replay_id for e in evs] == [rid(1), rid(2), rid(3)]


def test_reconnect_attempts_run_out(ps: PubSubHarness) -> None:
    ps.server.script(*[fails(grpc.StatusCode.UNAVAILABLE, "service.unavailable")] * 3)
    with pytest.raises(esf.PubSubError) as ei:
        ps.consume(n=1)
    assert type(ei.value) is esf.PubSubError
    assert ei.value.error_code == "sfdc.platform.eventbus.grpc.service.unavailable"
    assert ei.value.request_id == "rpc-fail-1"
    assert ei.value.operation == "pubsub.subscribe"
    assert ei.value.path == TOPIC
    assert ei.value.status is None
    assert len(ps.server.streams) == 3


def test_attempts_reset_after_an_event(ps: PubSubHarness) -> None:
    unavailable = fails(grpc.StatusCode.UNAVAILABLE)
    ps.server.script(
        unavailable,
        unavailable,
        sends(batch(order(1)), then=lambda s: s.abort(grpc.StatusCode.UNAVAILABLE)),
        unavailable,
        sends(batch(order(2))),
    )
    evs = ps.consume(n=2)
    assert [e.replay_id for e in evs] == [rid(1), rid(2)]


def test_clean_end_of_stream_reconnects(ps: PubSubHarness, rec: Recorder) -> None:
    ps.server.script(ends(), sends(batch(order(1))))
    (ev,) = ps.consume(n=1)
    assert ev.replay_id == rid(1)
    assert rec.named("sf.pubsub.reconnect")[0]["error_code"] == "OK"


def test_idle_close_resubscribes_without_counting_attempts(ps: PubSubHarness, rec: Recorder) -> None:
    # Everything requested was delivered, then the server closed the stream (as Salesforce does
    # after 60 s without a FetchRequest while nothing is pending).
    idle_close = sends(batch(order(1), order(2)), then=lambda s: s.abort(grpc.StatusCode.UNAVAILABLE))
    ps.server.script(idle_close, idle_close, idle_close, sends(batch(order(7))))
    evs = ps.consume(n=7 - 4, batch_size=2)
    assert len(evs) == 3
    reasons = [e["reason"] for e in rec.named("sf.pubsub.reconnect")]
    assert reasons[0] == "idle"
    assert all(e["delay_ms"] == 0 for e in rec.named("sf.pubsub.reconnect"))


def test_org_migrated_waits_30_minutes(ps: PubSubHarness, rec: Recorder) -> None:
    ps.server.script(fails(grpc.StatusCode.UNAVAILABLE, "org.migrated"), sends(batch(order(1))))
    ps.consume(n=1)
    assert ps.sf.sleeps == [1800.0]
    assert rec.named("sf.pubsub.reconnect")[0]["reason"] == "org_migrated"


# --- auth ----------------------------------------------------------------------------------


def test_auth_failure_refreshes_once_and_resubscribes(
    ps: PubSubHarness, mock: respx.MockRouter, rec: Recorder
) -> None:
    route = mock.post(TOKEN_URL).respond(200, json=token_body("AT-refreshed-0002"))
    ps.server.script(fails(grpc.StatusCode.UNAUTHENTICATED, "service.auth.error"), sends(batch(order(1))))
    (ev,) = ps.consume(n=1)
    assert ev.replay_id == rid(1)
    assert route.call_count == 1
    assert [s.metadata["accesstoken"] for s in ps.server.streams] == ["AT-initial-0001", "AT-refreshed-0002"]
    assert rec.named("sf.pubsub.reconnect")[0]["reason"] == "auth"
    assert ps.sf.sleeps == []


def test_second_auth_failure_raises_session_expired(ps: PubSubHarness, mock: respx.MockRouter) -> None:
    mock.post(TOKEN_URL).respond(200, json=token_body("AT-refreshed-0002"))
    auth_error = fails(grpc.StatusCode.UNAUTHENTICATED, "auth.headers.invalid")
    ps.server.script(auth_error, auth_error)
    with pytest.raises(esf.SessionExpired) as ei:
        ps.consume(n=1)
    assert ei.value.error_code == "sfdc.platform.eventbus.grpc.auth.headers.invalid"
    assert "AT-" not in str(ei.value)


def test_auth_failure_on_a_static_token_raises(ps: PubSubHarness, mock: respx.MockRouter) -> None:
    sf = ps.client(
        auth=esf.StaticToken(access_token="AT-static-1", instance_url=INSTANCE),
        token=False,
    )
    sf._runtime.transport.tokens[sf._runtime.key] = make_token("AT-static-1")
    ps.server.script(fails(grpc.StatusCode.UNAUTHENTICATED))
    with pytest.raises(esf.SessionExpired):
        ps.consume(n=1, sf=sf)


# --- typed errors ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "cls"),
    [
        (grpc.StatusCode.NOT_FOUND, "topic.not.found", esf.NotFound),
        (grpc.StatusCode.PERMISSION_DENIED, "topic.cannot.subscribe", esf.Forbidden),
        (grpc.StatusCode.PERMISSION_DENIED, "topic.meta.permission", esf.Forbidden),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, "concurrent.client.limit.exceeded", esf.PubSubLimitExceeded),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, "subscription.limit.exceeded", esf.PubSubLimitExceeded),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, "service.protection.rate.limit", esf.PubSubLimitExceeded),
        (grpc.StatusCode.FAILED_PRECONDITION, "something.new", esf.PubSubError),
    ],
)
def test_errors_are_typed(
    ps: PubSubHarness, status: grpc.StatusCode, code: str, cls: type[esf.SalesforceError]
) -> None:
    ps.server.script(fails(status, code, "nope"))
    with pytest.raises(cls) as ei:
        ps.consume(n=1)
    assert type(ei.value) is cls
    assert ei.value.error_code == f"sfdc.platform.eventbus.grpc.{code}"
    assert ei.value.request_id == "rpc-fail-1"
    assert ei.value.message == "nope"
    assert len(ps.server.streams) == 1


def test_tenant_license_is_retried_once(ps: PubSubHarness) -> None:
    license_error = fails(grpc.StatusCode.PERMISSION_DENIED, "tenant.license")
    ps.server.script(license_error, sends(batch(order(1))))
    assert len(ps.consume(n=1)) == 1

    ps.server.script(license_error, license_error)
    with pytest.raises(esf.Forbidden):
        ps.consume(n=1)


def test_rejected_replay_id_raises_replay_unavailable(ps: PubSubHarness) -> None:
    store = ps.store()
    ps.save(store, KEY, rid(3))
    ps.server.script(fails(grpc.StatusCode.INVALID_ARGUMENT, "subscription.fetch.replayid.corrupted"))
    with pytest.raises(esf.ReplayUnavailable) as ei:
        ps.consume(n=1, replay_store=store)
    assert ei.value.replay_id == rid(3)
    assert isinstance(ei.value, esf.PubSubError)
    assert ps.load(store, KEY) == rid(3)


@pytest.mark.parametrize(("fallback", "preset"), [("latest", 0), ("earliest", 1)])
def test_rejected_replay_id_falls_back(ps: PubSubHarness, rec: Recorder, fallback: str, preset: int) -> None:
    ps.server.script(
        fails(grpc.StatusCode.INVALID_ARGUMENT, "subscription.fetch.replayid.corrupted"),
        sends(batch(order(1))),
    )
    (ev,) = ps.consume(n=1, replay=rid(3), on_invalid_replay=fallback)
    assert ev.replay_id == rid(1)
    assert ps.server.streams[1].first.replay_preset == preset
    assert ps.server.streams[1].first.replay_id == b""
    assert rec.named("sf.pubsub.reconnect")[0]["reason"] == "invalid_replay"


def test_fallback_is_used_once(ps: PubSubHarness) -> None:
    corrupted = fails(grpc.StatusCode.INVALID_ARGUMENT, "subscription.fetch.replayid.corrupted")
    ps.server.script(corrupted, corrupted)
    with pytest.raises(esf.ReplayUnavailable):
        ps.consume(n=1, replay=rid(3), on_invalid_replay="latest")


# --- lifecycle -----------------------------------------------------------------------------


def test_closing_the_client_closes_the_channel(ps: PubSubHarness) -> None:
    ps.server.script(sends(batch(order(1))))
    ps.consume(n=1)
    t = ps.sf._transport
    assert t.pubsub is not None
    ps.h.run(ps.sf.aclose() if ps.h.is_async else ps.sf.close())
    assert t.pubsub is None


def test_children_share_the_channel(ps: PubSubHarness) -> None:
    child = ps.sf.with_auth(esf.StaticToken(access_token="AT-child", instance_url=INSTANCE))
    ps.server.script(sends(batch(order(1))), sends(batch(order(2))))
    ps.consume(n=1)
    first_channel = ps.sf._transport.pubsub.channel()
    child._runtime.transport.tokens[child._runtime.key] = make_token("AT-child")
    ps.consume(n=1, sf=child)
    assert child._transport.pubsub.channel() is first_channel
    assert ps.server.streams[1].metadata["accesstoken"] == "AT-child"
