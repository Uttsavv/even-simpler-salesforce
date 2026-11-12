"""Pub/Sub API acceptance against a real org (§19 "0.5", V79), on both clients.

Needs the ``[pubsub]`` extra, writes, and two pieces of org setup:

- a platform event ``ESF_Sandbox_Event__e`` with a text field ``Message__c`` (override the
  names with ``ESF_SANDBOX_PUBSUB_EVENT=<Event__e>.<Field__c>``);
- Change Data Capture on Account (Setup > Change Data Capture), for the CDC test.

The Date test also needs ``tests/sandbox/org/pubsub`` deployed (platform event
``ESF_Sandbox_Date_Event__e`` with Date ``Day__c`` and DateTime ``When__c``; Date and DateTime
fields on Account; permission set ``Esf_Sandbox_PubSub``); it skips when the event is missing.
Deploy that folder with ``sf.metadata.deploy`` as ``scripts/sandbox_setup_org.py`` does.

Set ``ESF_SANDBOX_PUBSUB=1`` to run these tests. Run with ``-s`` to print the schemas and raw
``ChangeEventHeader`` bitmaps, the evidence for V27 and V28.

The tests start from ``replay="earliest"`` and match events by the run's tag, so they don't
race the publish against the subscribe. On a busy org that reads up to 72 hours of events.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest

import even_simpler_salesforce as esf

from .conftest import Driver

pytest.importorskip("grpc")
pytest.importorskip("fastavro")

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        os.environ.get("ESF_SANDBOX_PUBSUB") != "1", reason="sandbox: set ESF_SANDBOX_PUBSUB=1"
    ),
]

EVENT, FIELD = os.environ.get("ESF_SANDBOX_PUBSUB_EVENT", "ESF_Sandbox_Event__e.Message__c").split(".")
TOPIC = f"/event/{EVENT}"
DEADLINE_S = 600.0


class Stream:
    """Steps a subscription by hand on either client."""

    def __init__(self, sb: Driver, sub: Any) -> None:
        self.sb = sb
        self.sub = sub

    def __enter__(self) -> Stream:
        self.sb.run(self.sub.__aenter__() if self.sb.is_async else self.sub.__enter__())
        return self

    def __exit__(self, *exc: Any) -> None:
        self.sb.run(self.sub.__aexit__(*exc) if self.sb.is_async else self.sub.__exit__(*exc))

    def next(self) -> esf.PubSubEvent:
        return self.sb.run(self.sub.__anext__() if self.sb.is_async else self.sub.__next__())

    def until(self, pred: Any) -> esf.PubSubEvent:
        start = time.monotonic()
        while time.monotonic() - start < DEADLINE_S:
            ev = self.next()
            if pred(ev):
                return ev
        raise AssertionError("event not received in time")


def drop_connection(sb: Driver, sub: Any) -> None:
    """Make the open stream fail like a dropped connection (``UNAVAILABLE``) on its next read.

    The real stream is cancelled, so the client must subscribe to Salesforce again.
    """
    from even_simpler_salesforce.core.pubsub import RpcFailure

    real = sub._stream
    real.cancel()

    class Dropped:
        def cancel(self) -> None:
            pass

    if sb.is_async:

        async def fail(*_: Any) -> Any:
            raise RpcFailure("UNAVAILABLE", "forced disconnect (test)")

    else:

        def fail(*_: Any) -> Any:
            raise RpcFailure("UNAVAILABLE", "forced disconnect (test)")

    dropped = Dropped()
    dropped.read = fail  # type: ignore[attr-defined]
    dropped.write = fail  # type: ignore[attr-defined]
    sub._stream = dropped


def publish(sb: Driver, sf: Any, message: str) -> None:
    result = sb.run(sf.platform_events.publish(EVENT, {FIELD: message}))
    assert result.ok


def store_for(sb: Driver) -> Any:
    return esf.AsyncInMemoryReplayStore() if sb.is_async else esf.InMemoryReplayStore()


@pytest.mark.usefixtures("writes")
def test_platform_event_resumes_after_a_forced_disconnect(sb: Driver, tag: str) -> None:
    sf = sb.client()
    prefix = f"{tag}-{sb.kind}-"
    publish(sb, sf, prefix + "1")
    store = store_for(sb)
    seen: list[str] = []

    def mine(ev: esf.PubSubEvent) -> bool:
        msg = ev.payload.get(FIELD)
        if isinstance(msg, str) and msg.startswith(prefix):
            seen.append(msg)
            return True
        return False

    with Stream(sb, sf.pubsub.subscribe(TOPIC, replay="earliest", replay_store=store, batch_size=5)) as s:
        first = s.until(mine)
        assert first.event_id  # platform events carry their EventUuid
        print("\nschema", first.schema_id, json.dumps(sf._transport.pubsub.schemas[first.schema_id].json))
        # Force a disconnect: the client must resubscribe after the last processed event.
        drop_connection(sb, s.sub)
        publish(sb, sf, prefix + "2")
        s.until(mine)
    assert sb.recorder.events, "no events recorded"
    reconnects = [e for e in sb.recorder.events if e.name == "sf.pubsub.reconnect"]
    assert reconnects, "the forced disconnect did not reconnect"

    # A restart resumes from the replay store, after event 2.
    publish(sb, sf, prefix + "3")
    with Stream(sb, sf.pubsub.subscribe(TOPIC, replay="earliest", replay_store=store, batch_size=5)) as s:
        s.until(mine)
    # No loss; a repeat would be at most one batch (5 events), and never event 1 again here.
    assert seen[:2] == [prefix + "1", prefix + "2"]
    assert seen[-1] == prefix + "3"
    assert seen.count(prefix + "1") == 1


@pytest.mark.usefixtures("writes")
def test_change_event_for_an_account_update(sb: Driver, tag: str) -> None:
    sf = sb.client()
    accounts = sf.sobject("Account")
    acc_id = sb.run(
        accounts.create(
            {"Name": f"{tag}-{sb.kind}", "Description": "to be nulled", "BillingStreet": "1 Main"}
        )
    )
    try:
        sb.run(
            accounts.update(
                acc_id,
                {"Phone": "555-0100", "BillingCity": "Paris", "Description": None, "BillingStreet": None},
            )
        )

        def update_of_mine(ev: esf.PubSubEvent) -> bool:
            ch = ev.change
            return ch is not None and acc_id in ch.record_ids and ch.change_type is esf.ChangeType.UPDATE

        with Stream(sb, sf.pubsub.subscribe("/data/AccountChangeEvent", replay="earliest")) as s:
            ev = s.until(update_of_mine)
        ch = ev.change
        assert ch is not None
        print("\nraw header", json.dumps(ev.payload["ChangeEventHeader"], default=str))
        print("decoded changed_fields", ch.changed_fields)
        # Salesforce sets ConsumerEvent.event.id on change events too (a UUID), although the
        # docs describe it for platform events only; the client passes it through.
        assert ev.event_id
        assert "Phone" in ch.changed_fields
        assert "BillingAddress.City" in ch.changed_fields
        assert "Description" in ch.nulled_fields
        assert "BillingAddress.Street" in ch.nulled_fields
        assert ch.entity_name == "Account"
        assert ch.transaction_key
    finally:
        sb.run(accounts.delete(acc_id))


@pytest.mark.usefixtures("writes")
def test_platform_event_date_and_datetime_fields(sb: Driver, tag: str) -> None:
    """V28/V77: a Date field arrives as epoch ms with ``doc`` ``Data:DateOnly:…`` and decodes to
    a ``date``; a DateTime field (``Data:DateTime:…``) decodes to an aware UTC ``datetime``."""
    from datetime import date, datetime, timezone

    sf = sb.client()
    msg = f"{tag}-{sb.kind}-date"
    try:
        result = sb.run(
            sf.platform_events.publish(
                "ESF_Sandbox_Date_Event__e",
                {"Message__c": msg, "Day__c": "2026-11-12", "When__c": "2026-11-12T10:30:00.000Z"},
            )
        )
    except esf.NotFound:
        pytest.skip("sandbox: deploy tests/sandbox/org/pubsub for ESF_Sandbox_Date_Event__e")
    assert result.ok
    sub = sf.pubsub.subscribe("/event/ESF_Sandbox_Date_Event__e", replay="earliest")
    with Stream(sb, sub) as s:
        ev = s.until(lambda e: e.payload.get("Message__c") == msg)
    print("\nschema", json.dumps(sf._transport.pubsub.schemas[ev.schema_id].json))
    assert ev.event_id
    assert ev.payload["Day__c"] == date(2026, 11, 12)
    assert ev.payload["When__c"] == datetime(2026, 11, 12, 10, 30, tzinfo=timezone.utc)


def test_unknown_topic_raises_not_found(sb: Driver) -> None:
    """V75: ``NOT_FOUND`` with ``error-code`` ``…topic.not.found`` and an ``rpc-id`` trailer."""
    sf = sb.client()
    with pytest.raises(esf.NotFound) as ei, Stream(sb, sf.pubsub.subscribe("/event/No_Such_Event__e")) as s:
        s.next()
    assert ei.value.error_code == "sfdc.platform.eventbus.grpc.topic.not.found"
    assert ei.value.request_id


def test_corrupted_replay_id_raises_replay_unavailable(sb: Driver) -> None:
    """V75: ``INVALID_ARGUMENT`` with ``…subscription.fetch.replayid.corrupted``."""
    sf = sb.client()
    sub = sf.pubsub.subscribe(TOPIC, replay=b"\x00\x01garbage")
    with pytest.raises(esf.ReplayUnavailable) as ei, Stream(sb, sub) as s:
        s.next()
    assert ei.value.error_code == "sfdc.platform.eventbus.grpc.subscription.fetch.replayid.corrupted"
    assert ei.value.replay_id == b"\x00\x01garbage"
    assert ei.value.request_id
