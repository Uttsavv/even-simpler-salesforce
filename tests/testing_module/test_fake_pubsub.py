"""``fake.pubsub`` (§14): emit, emit_change, fail_next and subscribe on both fakes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce
from even_simpler_salesforce.testing._pubsub import FAKE_ORG_ID, change_topic

TOPIC = "/event/Order_Event__e"


class Driver:
    """Runs the same scenario on the sync or the async fake."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.fake: Any = FakeSalesforce() if kind == "sync" else AsyncFakeSalesforce()

    def store(self) -> Any:
        return esf.InMemoryReplayStore() if self.kind == "sync" else esf.AsyncInMemoryReplayStore()

    def load(self, store: Any, key: str) -> Any:
        return store.load(key) if self.kind == "sync" else asyncio.run(store.load(key))

    def save(self, store: Any, key: str, value: bytes) -> None:
        if self.kind == "sync":
            store.save(key, value)
        else:
            asyncio.run(store.save(key, value))

    def consume(
        self,
        topic: str = TOPIC,
        *,
        limit: int | None = None,
        each: Callable[[Any], None] | None = None,
        **kw: Any,
    ) -> list[Any]:
        out: list[Any] = []
        sub = self.fake.pubsub.subscribe(topic, **kw)
        if self.kind == "sync":
            with sub as events:
                for ev in events:
                    out.append(ev)
                    if each:
                        each(ev)
                    if limit is not None and len(out) >= limit:
                        break
            return out

        async def go() -> None:
            async with sub as events:
                async for ev in events:
                    out.append(ev)
                    if each:
                        each(ev)
                    if limit is not None and len(out) >= limit:
                        break

        asyncio.run(go())
        return out


@pytest.fixture(params=["sync", "async"])
def d(request: pytest.FixtureRequest) -> Driver:
    return Driver(request.param)


def test_emit_and_subscribe(d: Driver) -> None:
    first = d.fake.pubsub.emit(TOPIC, {"Order_Number__c": "O-1"})
    d.fake.pubsub.emit(TOPIC, {"Order_Number__c": "O-2"}, event_id="uuid-2")
    d.fake.pubsub.emit("/event/Other__e", {"x": 1})
    evs = d.consume()
    assert [e.payload["Order_Number__c"] for e in evs] == ["O-1", "O-2"]
    assert evs[0] == first
    assert evs[0].event_id  # a random EventUuid
    assert evs[1].event_id == "uuid-2"
    assert evs[0].change is None
    assert evs[0].replay_id < evs[1].replay_id
    assert d.fake.calls[-1].operation == "pubsub.subscribe"
    assert len(d.fake.pubsub.emitted(TOPIC)) == 2
    assert "Order_Event__e" in repr(d.fake.pubsub)


def test_payload_is_copied(d: Driver) -> None:
    data = {"n": [1]}
    d.fake.pubsub.emit(TOPIC, data)
    data["n"].append(2)
    assert d.consume()[0].payload == {"n": [1]}


def test_emit_change(d: Driver) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    d.fake.pubsub.emit_change(
        "Account",
        "UPDATE",
        ["001xx0000000001AAA"],
        changed_fields=["Phone", "BillingAddress.City"],
        nulled_fields=["Fax"],
        values={"Phone": "555-0100"},
        transaction_key="tx-1",
        commit_timestamp=when,
    )
    d.fake.pubsub.emit_change("Account", esf.ChangeType.GAP_OVERFLOW)
    d.fake.pubsub.emit_change("Order__c", "CREATE", "a01xx0000000001AAA")
    upd, overflow = d.consume("/data/AccountChangeEvent")
    assert upd.event_id is None
    assert upd.change.change_type is esf.ChangeType.UPDATE
    assert upd.change.changed_fields == ("Phone", "BillingAddress.City")
    assert upd.change.nulled_fields == ("Fax",)
    assert upd.change.record_ids == ("001xx0000000001AAA",)
    assert upd.change.transaction_key == "tx-1"
    assert upd.change.commit_timestamp == when
    assert upd.payload["Phone"] == "555-0100"
    assert upd.payload["ChangeEventHeader"]["changeType"] == "UPDATE"
    assert overflow.change.is_overflow
    assert overflow.change.record_ids == ()
    assert overflow.payload["ChangeEventHeader"]["recordIds"] == ["000000000000000AAA"]
    (custom,) = d.consume("/data/Order__ChangeEvent")
    assert custom.change.record_ids == ("a01xx0000000001AAA",)


def test_emit_change_rejects_unknown_types(d: Driver) -> None:
    with pytest.raises(ValueError):
        d.fake.pubsub.emit_change("Account", "TELEPORT")


def test_change_topic() -> None:
    assert change_topic("Account") == "/data/AccountChangeEvent"
    assert change_topic("Order__c") == "/data/Order__ChangeEvent"


def test_replay_store_save_points(d: Driver) -> None:
    store = d.store()
    key = f"{FAKE_ORG_ID}:{TOPIC}"
    for i in range(5):
        d.fake.pubsub.emit(TOPIC, {"n": i})
    seen: list[Any] = []
    d.consume(replay_store=store, batch_size=2, each=lambda ev: seen.append(store._data.get(key)))
    ids = [e.replay_id for e in d.fake.pubsub.emitted(TOPIC)]
    assert seen == [None, None, ids[1], ids[1], ids[3]]
    assert d.load(store, key) == ids[4]

    # Resuming starts after the stored position.
    d.fake.pubsub.emit(TOPIC, {"n": 5})
    assert [e.payload["n"] for e in d.consume(replay_store=store)] == [5]


def test_no_save_when_the_block_raises(d: Driver) -> None:
    store = d.store()
    d.fake.pubsub.emit(TOPIC, {"n": 0})
    d.fake.pubsub.emit(TOPIC, {"n": 1})

    def boom(ev: Any) -> None:
        if ev.payload["n"] == 1:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        d.consume(replay_store=store, each=boom, batch_size=1)
    assert d.load(store, f"{FAKE_ORG_ID}:{TOPIC}") == d.fake.pubsub.emitted(TOPIC)[0].replay_id


def test_break_counts_the_current_event(d: Driver) -> None:
    store = d.store()
    for i in range(3):
        d.fake.pubsub.emit(TOPIC, {"n": i})
    d.consume(replay_store=store, limit=1)
    assert d.load(store, f"{FAKE_ORG_ID}:{TOPIC}") == d.fake.pubsub.emitted(TOPIC)[0].replay_id


def test_replay_id_argument(d: Driver) -> None:
    a = d.fake.pubsub.emit(TOPIC, {"n": 0})
    d.fake.pubsub.emit(TOPIC, {"n": 1})
    assert [e.payload["n"] for e in d.consume(replay=a.replay_id)] == [1]
    assert [e.payload["n"] for e in d.consume(replay="earliest")] == [0, 1]


def test_unknown_replay_id(d: Driver) -> None:
    d.fake.pubsub.emit(TOPIC, {"n": 0})
    with pytest.raises(esf.ReplayUnavailable) as ei:
        d.consume(replay=b"\x09")
    assert ei.value.replay_id == b"\x09"
    assert ei.value.error_code.endswith("replayid.corrupted")
    assert ei.value.path == TOPIC
    assert [e.payload["n"] for e in d.consume(replay=b"\x09", on_invalid_replay="latest")] == [0]

    store = d.store()
    d.save(store, f"{FAKE_ORG_ID}:{TOPIC}", b"\x09")
    assert len(d.consume(replay_store=store, on_invalid_replay="earliest")) == 1


def test_fail_next(d: Driver) -> None:
    d.fake.pubsub.emit(TOPIC, {"n": 0})
    d.fake.pubsub.fail_next(esf.PubSubLimitExceeded("too many clients"), topic=TOPIC)
    with pytest.raises(esf.PubSubLimitExceeded) as ei:
        d.consume()
    assert ei.value.operation == "pubsub.subscribe"
    assert ei.value.path == TOPIC
    assert len(d.consume()) == 1

    d.fake.pubsub.fail_next(RuntimeError, topic="/event/Other__e")
    assert len(d.consume()) == 1
    d.fake.fail_next("pubsub.subscribe", esf.ReplayUnavailable)
    with pytest.raises(esf.ReplayUnavailable):
        d.consume()


def test_events_are_emitted(d: Driver) -> None:
    rec: list[esf.Event] = []

    class Obs:
        def on_event(self, event: esf.Event) -> None:
            rec.append(event)

    d.fake.observers.add(Obs())
    d.fake.pubsub.emit(TOPIC, {"n": 0})
    d.consume()
    names = [e.name for e in rec]
    assert names == ["sf.pubsub.subscribed", "sf.pubsub.closed"]
    assert rec[1]["events"] == 1
    assert rec[1]["outcome"] == "ok"


def test_subscription_lifecycle(d: Driver) -> None:
    sub = d.fake.pubsub.subscribe(TOPIC)
    assert "new" in repr(sub)
    assert sub.topic == TOPIC
    if d.kind == "sync":
        with sub:
            pass
        assert list(sub) == []
        with pytest.raises(RuntimeError), sub:
            pass
        sub.__exit__(None, None, None)  # closing twice is a no-op
    else:

        async def go() -> None:
            async with sub:
                pass
            assert [e async for e in sub] == []
            with pytest.raises(RuntimeError):
                async with sub:
                    pass
            await sub.__aexit__(None, None, None)

        asyncio.run(go())
    assert "closed" in repr(sub)


def test_subscribe_checks_arguments(d: Driver) -> None:
    with pytest.raises(ValueError, match="topic"):
        d.fake.pubsub.subscribe("Order_Event__e")
    with pytest.raises(ValueError, match="batch_size"):
        d.fake.pubsub.subscribe(TOPIC, batch_size=0)
    with pytest.raises(ValueError, match="topic"):
        d.fake.pubsub.emit("nope", {})


def test_closed_fake_refuses_subscriptions() -> None:
    fake = FakeSalesforce()
    fake.close()
    with pytest.raises(esf.TransportError), fake.pubsub.subscribe(TOPIC):
        pass


def test_children_share_events() -> None:
    fake = FakeSalesforce()
    child = fake.with_auth(esf.StaticToken("y", "https://acme.my.salesforce.com"))
    fake.pubsub.emit(TOPIC, {"n": 0})
    with child.pubsub.subscribe(TOPIC) as events:
        assert [e.payload for e in events] == [{"n": 0}]
