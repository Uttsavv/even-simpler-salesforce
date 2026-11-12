"""Fake of ``sf.pubsub`` (§14): emit events in a test, consume them with ``subscribe``.

No gRPC, protobuf or Avro is involved, so it works without the ``[pubsub]`` extra. The fake
keeps the client's delivery rules (replay store save points, replay keys, rejected replay ids,
injected errors), so code tested against it behaves the same against Salesforce.
"""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any

from ..core import pubsub as ps
from ..errors import ReplayUnavailable, SalesforceError
from ..models import ChangeHeader, ChangeType, PubSubEvent
from ._state import FakeCall, FakeState, Who

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .._async.replay_store import AsyncReplayStore
    from .._sync.replay_store import ReplayStore

__all__ = ["AsyncFakePubSub", "AsyncFakeSubscription", "FAKE_ORG_ID", "FakePubSub", "FakeSubscription"]

#: The org id the fakes report (``userinfo``), used in replay-store keys.
FAKE_ORG_ID = "00D000000000001AAA"
_SCHEMA_ID = "fake-schema"
_CORRUPTED = "sfdc.platform.eventbus.grpc.subscription.fetch.replayid.corrupted"


def change_topic(entity: str) -> str:
    """The single-entity change event channel: ``Account`` -> ``/data/AccountChangeEvent``,
    ``Order__c`` -> ``/data/Order__ChangeEvent``."""
    base = entity[:-3] + "__" if entity.endswith("__c") else entity
    return f"/data/{base}ChangeEvent"


class FakePubSub:
    """Fake of ``sf.pubsub`` on :class:`~even_simpler_salesforce.testing.FakeSalesforce`.

    Emit events with :meth:`emit` and :meth:`emit_change`; ``fake.pubsub.subscribe(topic)``
    then yields them, like the real client. Differences from the real client:

    - The fake has no history before the test: ``replay="latest"`` and ``"earliest"`` both
      start at the first event emitted on the topic.
    - Iterating ends when every emitted event was yielded, instead of waiting for more.
    - Change events carry field names in their ``ChangeEventHeader`` (no bitmaps).

    Example:
        fake.pubsub.emit_change("Account", "UPDATE", ["001..."], changed_fields=["Phone"])
        with fake.pubsub.subscribe("/data/AccountChangeEvent") as events:
            for ev in events:
                handle(ev)
    """

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    def __repr__(self) -> str:
        return f"<{type(self).__name__} topics={sorted(self._state.pubsub)}>"

    # --- test controls ---------------------------------------------------------------

    def emit(
        self,
        topic: str,
        payload: Mapping[str, Any],
        *,
        event_id: str | None = None,
        schema_id: str = _SCHEMA_ID,
        change: ChangeHeader | None = None,
    ) -> PubSubEvent:
        """Add an event to ``topic``; subscriptions yield it in order.

        Args:
            topic: ``"/event/Order_Event__e"`` or a change event channel.
            payload: The decoded event fields.
            event_id: The event's ``id`` (``EventUuid``). Defaults to a random UUID, on change
                event channels too, as Salesforce sends.
            schema_id: Reported as :attr:`PubSubEvent.schema_id`.
            change: The decoded header of a change event; prefer :meth:`emit_change`.

        Returns:
            The event as subscriptions will yield it, with a fresh replay id.
        """
        ps.check_subscribe(topic, "latest", "raise", 1)
        if event_id is None:
            event_id = str(uuid.uuid4())
        with self._state.lock:
            self._state.pubsub_seq += 1
            event = PubSubEvent(
                topic=topic,
                replay_id=self._state.pubsub_seq.to_bytes(8, "big"),
                event_id=event_id,
                schema_id=schema_id,
                payload=copy.deepcopy(dict(payload)),
                change=change,
            )
            self._state.pubsub.setdefault(topic, []).append(event)
        return event

    def emit_change(
        self,
        entity: str,
        change_type: ChangeType | str,
        record_ids: Iterable[str] = (),
        *,
        changed_fields: Iterable[str] = (),
        nulled_fields: Iterable[str] = (),
        diff_fields: Iterable[str] = (),
        values: Mapping[str, Any] | None = None,
        topic: str | None = None,
        transaction_key: str | None = None,
        sequence_number: int = 1,
        commit_user: str | None = None,
        change_origin: str | None = None,
        commit_timestamp: datetime | None = None,
    ) -> PubSubEvent:
        """Add a Change Data Capture event.

        Args:
            entity: The sObject, e.g. ``"Account"``.
            change_type: ``"CREATE"``, ``"UPDATE"``, ..., ``"GAP_UPDATE"``, ``"GAP_OVERFLOW"``.
            record_ids: The changed records (empty for ``GAP_OVERFLOW``).
            changed_fields: Field names, ``Parent.Child`` for compound components.
            nulled_fields: Fields set to ``null``.
            diff_fields: Long text fields sent as a diff.
            values: Field values in the payload (leave out for gap events).
            topic: Defaults to the entity's channel, e.g. ``/data/AccountChangeEvent``. Pass
                ``"/data/ChangeEvents"`` to emit on the standard channel.
            transaction_key: Defaults to a random key per event.
            sequence_number: Position in the transaction.
            commit_user: Id of the user who made the change.
            change_origin: Client that made the change.
            commit_timestamp: Defaults to the fake's clock.

        Returns:
            The event as subscriptions will yield it.
        """
        kind = ChangeType(change_type)
        ids = (record_ids,) if isinstance(record_ids, str) else tuple(record_ids)
        header = ChangeHeader(
            entity_name=entity,
            change_type=kind,
            record_ids=tuple(i for i in ids if i != ps.EMPTY_RECORD_ID),
            changed_fields=tuple(changed_fields),
            nulled_fields=tuple(nulled_fields),
            diff_fields=tuple(diff_fields),
            transaction_key=transaction_key or uuid.uuid4().hex,
            sequence_number=sequence_number,
            commit_timestamp=commit_timestamp or self._state.clock(),
            commit_user=commit_user,
            change_origin=change_origin,
        )
        payload = {
            "ChangeEventHeader": {
                "entityName": entity,
                "recordIds": list(ids) or ([ps.EMPTY_RECORD_ID] if kind is ChangeType.GAP_OVERFLOW else []),
                "changeType": kind.value,
                "changeOrigin": change_origin or "",
                "transactionKey": header.transaction_key,
                "sequenceNumber": sequence_number,
                "commitTimestamp": int(header.commit_timestamp.timestamp() * 1000),  # pyright: ignore[reportOptionalMemberAccess]
                "commitUser": commit_user or "",
                "changedFields": list(header.changed_fields),
                "nulledFields": list(header.nulled_fields),
                "diffFields": list(header.diff_fields),
            },
            **dict(values or {}),
        }
        return self.emit(topic or change_topic(entity), payload, event_id=None, change=header)

    def fail_next(
        self, error: BaseException | type[BaseException], *, topic: str | None = None, times: int = 1
    ) -> None:
        """Make the next read of a subscription raise ``error``.

        Like the real client, the error comes out of the ``for`` loop, for example
        ``esf.ReplayUnavailable(...)`` or ``esf.PubSubLimitExceeded(...)``. Same as
        ``fake.fail_next("pubsub.subscribe", error, sobject=topic)``.

        Args:
            error: The exception (instance or class).
            topic: Only fail subscriptions to this topic.
            times: Number of reads to fail.
        """
        self._state.add_failure(ps.OPERATION, error, topic, times)

    def emitted(self, topic: str) -> list[PubSubEvent]:
        """Every event emitted on ``topic`` so far."""
        with self._state.lock:
            return list(self._state.pubsub.get(topic, ()))

    # --- client API ------------------------------------------------------------------

    def subscribe(
        self,
        topic: str,
        *,
        replay: ps.Replay = "latest",
        replay_store: ReplayStore | None = None,
        on_invalid_replay: ps.OnInvalidReplay = "raise",
        batch_size: int = ps.MAX_BATCH_SIZE,
    ) -> FakeSubscription:
        """Same arguments and delivery rules as ``Salesforce.pubsub.subscribe``."""
        ps.check_subscribe(topic, replay, on_invalid_replay, batch_size)
        return FakeSubscription(
            _Cursor(self._state, self._who, topic, replay, on_invalid_replay, batch_size), replay_store
        )


class _Cursor:
    """The delivery state of one fake subscription, shared by the sync and async wrappers."""

    def __init__(
        self,
        state: FakeState,
        who: Who,
        topic: str,
        replay: ps.Replay,
        on_invalid: ps.OnInvalidReplay,
        batch_size: int,
    ) -> None:
        self.state = state
        self.who = who
        self.topic = topic
        self.replay: ps.Replay = replay
        self.on_invalid = on_invalid
        self.batch_size = batch_size
        self.key = ps.replay_key(FAKE_ORG_ID, topic)
        self.tracker = ps.ReplayTracker()
        self.index = 0
        self.in_batch = 0
        self.rejected: bytes | None = None
        self.current: PubSubEvent | None = None
        self.current_end = False
        self.state_name = "new"
        self.events = 0
        self.started = 0.0

    def open(self, stored: bytes | None) -> None:
        if self.state_name != "new":
            raise RuntimeError("a subscription can only be opened once; call subscribe() again")
        self.state_name = "open"
        self.started = time.perf_counter()
        caller, priority = self.who.resolve()
        preset, replay_id, resumed_from = ps.start_position(stored, self.replay)
        with self.state.lock:
            if self.state.closed:
                from ..errors import TransportError

                raise TransportError("client is closed", operation=ps.OPERATION, retryable=False)
            self.state.calls.append(
                FakeCall(
                    ps.OPERATION, self.topic, {"topic": self.topic, "replay": self.replay}, caller, priority
                )
            )
            log = self.state.pubsub.get(self.topic, [])
            if replay_id is not None:
                found = next((i for i, e in enumerate(log) if e.replay_id == replay_id), None)
                if found is None:
                    self.rejected = replay_id
                else:
                    self.index = found + 1
        self.state._emit(  # pyright: ignore[reportPrivateUsage]
            "sf.pubsub.subscribed",
            {
                "topic": self.topic,
                "replay_preset": preset,
                "replay_id": ps.replay_hex(replay_id),
                "resumed_from": resumed_from,
                "stream_no": 1,
                "batch_size": self.batch_size,
                "schema_calls": 0,
            },
        )

    def processed(self) -> bytes | None:
        """Mark the current event processed; the replay id to save, if any."""
        current, self.current = self.current, None
        if current is None:
            return None
        return self.tracker.processed(current.replay_id, end_of_batch=self.current_end)

    def next(self) -> PubSubEvent | None:
        """The next event, or ``None`` when every emitted event was yielded."""
        if self.state_name != "open":
            return None
        injected = self.state._pop_failure(ps.OPERATION, self.topic)  # pyright: ignore[reportPrivateUsage]
        if injected is not None:
            if isinstance(injected, SalesforceError):
                injected.operation = injected.operation or ps.OPERATION
                injected.path = injected.path or self.topic
            raise injected
        if self.rejected is not None:
            rejected, self.rejected = self.rejected, None
            if self.on_invalid == "raise":
                raise ReplayUnavailable(
                    "replay id not found (fake): it isn't one of the events emitted on this topic",
                    replay_id=rejected,
                    error_code=_CORRUPTED,
                    operation=ps.OPERATION,
                    path=self.topic,
                )
            self.index = 0  # the fake has no history: both presets start at the first event
        with self.state.lock:
            log = self.state.pubsub.get(self.topic, [])
            if self.index >= len(log):
                return None
            event = log[self.index]
            self.index += 1
            self.in_batch += 1
            self.current_end = self.in_batch >= self.batch_size or self.index >= len(log)
            if self.current_end:
                self.in_batch = 0
        self.current = event
        self.events += 1
        return event

    def close(self, exc: BaseException | None) -> None:
        self.state_name = "closed"
        self.state._emit(  # pyright: ignore[reportPrivateUsage]
            "sf.pubsub.closed",
            {
                "topic": self.topic,
                "events": self.events,
                "reconnects": 0,
                "duration_ms": round((time.perf_counter() - self.started) * 1000, 1),
                "outcome": "ok" if exc is None else "error",
                "error_class": type(exc).__name__ if exc is not None else None,
            },
        )


class FakeSubscription:
    """What ``fake.pubsub.subscribe()`` returns: use it as ``with ... as events: for ev in events``."""

    def __init__(self, cursor: _Cursor, store: ReplayStore | None) -> None:
        self._c = cursor
        self._store = store

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._c.topic} {self._c.state_name} events={self._c.events}>"

    @property
    def topic(self) -> str:
        """The subscribed topic."""
        return self._c.topic

    def _save(self, replay_id: bytes | None) -> None:
        if replay_id is not None and self._store is not None:
            self._store.save(self._c.key, replay_id)

    def __enter__(self) -> FakeSubscription:
        stored = (
            self._store.load(self._c.key) if self._store is not None and self._c.state_name == "new" else None
        )
        self._c.open(stored)
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._c.state_name == "closed":
            return
        try:
            if exc is None:
                self._save(self._c.processed())
                self._save(self._c.tracker.finish())
        finally:
            self._c.close(exc)

    def __iter__(self) -> Iterator[PubSubEvent]:
        return self

    def __next__(self) -> PubSubEvent:
        self._save(self._c.processed())
        event = self._c.next()
        if event is None:
            raise StopIteration
        return event


class AsyncFakeSubscription:
    """What ``AsyncFakeSalesforce.pubsub.subscribe()`` returns (``async with`` / ``async for``)."""

    def __init__(self, cursor: _Cursor, store: AsyncReplayStore | None) -> None:
        self._c = cursor
        self._store = store

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._c.topic} {self._c.state_name} events={self._c.events}>"

    @property
    def topic(self) -> str:
        """The subscribed topic."""
        return self._c.topic

    async def _save(self, replay_id: bytes | None) -> None:
        if replay_id is not None and self._store is not None:
            await self._store.save(self._c.key, replay_id)

    async def __aenter__(self) -> AsyncFakeSubscription:
        stored = (
            await self._store.load(self._c.key)
            if self._store is not None and self._c.state_name == "new"
            else None
        )
        self._c.open(stored)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._c.state_name == "closed":
            return
        try:
            if exc is None:
                await self._save(self._c.processed())
                await self._save(self._c.tracker.finish())
        finally:
            self._c.close(exc)

    def __aiter__(self) -> AsyncIterator[PubSubEvent]:
        return self

    async def __anext__(self) -> PubSubEvent:
        await self._save(self._c.processed())
        event = self._c.next()
        if event is None:
            raise StopAsyncIteration
        return event


class AsyncFakePubSub:
    """Fake of ``sf.pubsub`` on :class:`~even_simpler_salesforce.testing.AsyncFakeSalesforce`.

    The test controls (:meth:`emit`, :meth:`emit_change`, :meth:`fail_next`, :meth:`emitted`)
    are plain methods, shared with :class:`FakePubSub`; see there for the behaviour.
    """

    def __init__(self, inner: FakePubSub) -> None:
        self._inner = inner
        self.emit = inner.emit
        self.emit_change = inner.emit_change
        self.fail_next = inner.fail_next
        self.emitted = inner.emitted

    def __repr__(self) -> str:
        return f"<{type(self).__name__} topics={sorted(self._inner._state.pubsub)}>"  # pyright: ignore[reportPrivateUsage]

    def subscribe(
        self,
        topic: str,
        *,
        replay: ps.Replay = "latest",
        replay_store: AsyncReplayStore | None = None,
        on_invalid_replay: ps.OnInvalidReplay = "raise",
        batch_size: int = ps.MAX_BATCH_SIZE,
    ) -> AsyncFakeSubscription:
        """Same arguments and delivery rules as ``AsyncSalesforce.pubsub.subscribe``."""
        ps.check_subscribe(topic, replay, on_invalid_replay, batch_size)
        inner = self._inner
        return AsyncFakeSubscription(
            _Cursor(inner._state, inner._who, topic, replay, on_invalid_replay, batch_size),  # pyright: ignore[reportPrivateUsage]
            replay_store,
        )
