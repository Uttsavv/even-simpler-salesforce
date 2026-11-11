"""The gRPC path for the Pub/Sub API (§5.1, §9.16, D31).

gRPC streams can't go through the httpx transport, so this module is its equivalent for
``sf.pubsub``. It reuses the client's auth runtime (one shared refresh), ``RetryPolicy`` backoff
and observers. Every decision is a pure function in :mod:`..core.pubsub`; the grpc calls
themselves go through ``_compat`` (``GrpcStream``, ``grpc_unary``), so the generated sync twin
only swaps that module.

Importing this module needs the ``[pubsub]`` extra (protobuf).
"""

from __future__ import annotations

import time
from collections import deque
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

from .._pubsub import pubsub_api_pb2 as pb
from ..core import pubsub as ps
from ..core.retry import backoff_delay
from ..errors import ReplayUnavailable, UnexpectedResponse
from ..models import PubSubEvent
from . import _compat

if TYPE_CHECKING:
    from ..auth.token import Token
    from .client import AsyncSalesforce
    from .replay_store import AsyncReplayStore

__all__ = ["AsyncPubSubTransport", "AsyncSubscription"]

SUBSCRIBE = "/eventbus.v1.PubSub/Subscribe"
GET_SCHEMA = "/eventbus.v1.PubSub/GetSchema"
SCHEMA_TIMEOUT = 30.0


class AsyncPubSubTransport:
    """gRPC state shared by a client family: one channel, the schema cache and org ids.

    The channel opens on the first subscription and closes with the client. One channel carries
    at most 1,000 concurrent streams; gRPC queues more in the client.
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._channel: Any = None
        #: Parsed schemas by schema id, for the client's lifetime.
        self.schemas: dict[str, ps.Schema] = {}
        #: Org id per token key, for tokens without one (``StaticToken``).
        self.org_ids: dict[str, str] = {}

    def channel(self) -> Any:
        if self._channel is None:
            self._channel = _compat.grpc_channel(self.endpoint)
        return self._channel

    async def aclose(self) -> None:
        channel, self._channel = self._channel, None
        if channel is not None:
            await _compat.grpc_close(channel)


class AsyncSubscription:
    """An open subscription: iterate it to receive :class:`PubSubEvent` objects.

    Returned by ``sf.pubsub.subscribe()``; use it as ``async with ... as events: async for ev in
    events``. Iterating is the flow control: the client requests more events only after you
    have taken the ones it holds. An event counts as processed when you ask for the next one,
    or when the block exits normally.
    """

    def __init__(
        self,
        client: AsyncSalesforce,
        topic: str,
        *,
        replay: ps.Replay,
        replay_store: AsyncReplayStore | None,
        on_invalid_replay: ps.OnInvalidReplay,
        batch_size: int,
    ) -> None:
        self._sf = client
        self._t = client._transport
        self._runtime = client._runtime
        self.topic = topic
        self._replay: ps.Replay = replay
        self._store = replay_store
        self._on_invalid = on_invalid_replay
        self._batch_size = batch_size
        self._pst: AsyncPubSubTransport | None = None
        self._state = "new"
        self._stream: Any = None
        self._buffer: deque[tuple[PubSubEvent, bool]] = deque()  # (event, last of its batch)
        self._current: tuple[PubSubEvent, bool] | None = None
        self._tracker = ps.ReplayTracker()
        self._pending = 0
        self._key: str | None = None
        self._start: tuple[ps.Preset, bytes | None, str] | None = None
        self._last_start: tuple[ps.Preset, bytes | None] = ("LATEST", None)
        self._metadata: tuple[tuple[str, str], ...] = ()
        self._token: Token | None = None
        self._generation = 0
        self._attempts = 0
        self._auth_retried = False
        self._license_retried = False
        self._fallback_used = False
        self._stream_no = 0
        self._events = 0
        self._reconnects = 0
        self._schema_calls = 0
        self._started = 0.0

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.topic} {self._state} events={self._events}>"

    # --- lifecycle -------------------------------------------------------------------

    async def __aenter__(self) -> AsyncSubscription:
        if self._state != "new":
            raise RuntimeError("a subscription can only be opened once; call sf.pubsub.subscribe() again")
        self._state = "open"
        self._started = time.perf_counter()
        t = self._t
        if t.pubsub is None:
            t.pubsub = AsyncPubSubTransport(t.pubsub_endpoint or ps.DEFAULT_ENDPOINT)
        self._pst = t.pubsub
        try:
            try:
                await self._connect()
            except ps.RpcFailure as failure:
                await self._recover(failure, idle=False)
        except BaseException as exc:
            await self._close(exc)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._close(exc)

    def __aiter__(self) -> AsyncSubscription:
        return self

    async def __anext__(self) -> PubSubEvent:
        if self._state != "open":
            raise StopAsyncIteration
        await self._processed()
        while not self._buffer:
            await self._fill()
        item = self._buffer.popleft()
        self._current = item
        self._events += 1
        return item[0]

    async def _close(self, exc: BaseException | None) -> None:
        if self._state == "closed":
            return
        try:
            if exc is None and self._state == "open":
                # A normal exit (including ``break``): the current event counts as processed.
                await self._processed()
                await self._save(self._tracker.finish())
        finally:
            self._state = "closed"
            self._drop_stream()
            self._t.emit(
                "sf.pubsub.closed",
                {
                    "topic": self.topic,
                    "events": self._events,
                    "reconnects": self._reconnects,
                    "duration_ms": round((time.perf_counter() - self._started) * 1000, 1),
                    "outcome": "ok" if exc is None else "error",
                    "error_class": type(exc).__name__ if exc is not None else None,
                },
            )

    # --- replay ----------------------------------------------------------------------

    async def _save(self, replay_id: bytes | None) -> None:
        if replay_id is not None and self._store is not None and self._key is not None:
            await self._store.save(self._key, replay_id)

    async def _processed(self) -> None:
        current, self._current = self._current, None
        if current is not None:
            event, end_of_batch = current
            await self._save(self._tracker.processed(event.replay_id, end_of_batch=end_of_batch))

    # --- stream ----------------------------------------------------------------------

    def _drop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.cancel()
        self._buffer.clear()  # dropped events are redelivered: the next stream resumes before them
        self._pending = 0

    async def _org_id(self, token: Token) -> str:
        if token.org_id:
            return token.org_id
        assert self._pst is not None
        key = self._runtime.key
        org_id = self._pst.org_ids.get(key)
        if org_id is None:
            info = await self._sf.auth.userinfo()
            found = info.get("organization_id")
            if not isinstance(found, str) or not found:
                raise UnexpectedResponse("userinfo has no organization_id", operation=ps.OPERATION)
            org_id = self._pst.org_ids[key] = found
        return org_id

    async def _connect(self) -> None:
        assert self._pst is not None
        token = await self._runtime.token()
        self._generation = self._runtime.generation()
        self._token = token
        org_id = await self._org_id(token)
        instance_url = self._sf.instance_url or token.instance_url
        self._metadata = (
            ("accesstoken", token.access_token),
            ("instanceurl", instance_url),
            ("tenantid", org_id),
        )
        if self._key is None:
            self._key = ps.replay_key(org_id, self.topic)
        if self._start is None:
            stored = await self._store.load(self._key) if self._store is not None else None
            self._start = ps.start_position(stored, self._replay)
        if self._tracker.position is not None:
            preset, replay_id, resumed_from = "CUSTOM", self._tracker.position, "memory"
        else:
            preset, replay_id, resumed_from = self._start
        request = pb.FetchRequest(
            topic_name=self.topic,
            replay_preset=preset,
            num_requested=self._batch_size,
        )
        if replay_id is not None:
            request.replay_id = replay_id
        self._stream = _compat.GrpcStream(
            self._pst.channel(),
            SUBSCRIBE,
            metadata=self._metadata,
            request_type=pb.FetchRequest,
            response_type=pb.FetchResponse,
        )
        self._stream_no += 1
        self._last_start = (preset, replay_id)
        await self._stream.write(request)  # only the first request may set the position
        self._pending = self._batch_size
        self._t.emit(
            "sf.pubsub.subscribed",
            {
                "topic": self.topic,
                "replay_preset": preset,
                "replay_id": ps.replay_hex(replay_id),
                "resumed_from": resumed_from,
                "stream_no": self._stream_no,
                "batch_size": self._batch_size,
                "schema_calls": self._schema_calls,
            },
        )

    async def _fill(self) -> None:
        """Get the next response into the buffer, recovering from a failed stream."""
        idle = False
        try:
            if self._stream is None:
                await self._connect()
            stream: Any = self._stream
            # Nothing was requested while the consumer worked: Salesforce may have closed the
            # stream after 60 seconds without a FetchRequest.
            idle = self._pending == 0
            more = ps.refill(self._batch_size, self._pending, len(self._buffer))
            if more:
                await stream.write(pb.FetchRequest(num_requested=more))
                self._pending += more
            response = await stream.read()
            if response is None:
                raise ps.RpcFailure("OK")
            await self._receive(response)
        except ps.RpcFailure as failure:
            await self._recover(failure, idle=idle)

    async def _receive(self, response: Any) -> None:
        events = list(response.events)
        if not events:  # keepalive: never yielded
            drained = not self._buffer and self._current is None
            await self._save(self._tracker.keepalive(bytes(response.latest_replay_id), drained=drained))
            return
        self._pending = max(0, self._pending - len(events))
        decoded = [await self._decode(e) for e in events]
        for i, event in enumerate(decoded):
            self._buffer.append((event, i == len(decoded) - 1))
        # A stream that delivers is healthy again.
        self._attempts = 0
        self._auth_retried = False
        self._license_retried = False
        newest = ps.event_time(decoded[-1].payload, decoded[-1].change)
        self._t.emit(
            "sf.pubsub.batch",
            {
                "topic": self.topic,
                "events": len(decoded),
                "pending": self._pending,
                "lag_ms": round((self._t.now() - newest).total_seconds() * 1000, 1) if newest else None,
            },
        )

    async def _schema(self, schema_id: str) -> ps.Schema:
        assert self._pst is not None
        cached = self._pst.schemas.get(schema_id)
        if cached is not None:
            return cached
        info = await _compat.grpc_unary(
            self._pst.channel(),
            GET_SCHEMA,
            pb.SchemaRequest(schema_id=schema_id),
            metadata=self._metadata,
            response_type=pb.SchemaInfo,
            timeout=SCHEMA_TIMEOUT,
        )
        self._schema_calls += 1
        schema = ps.parse_schema(schema_id, info.schema_json, self._t.loads)
        self._pst.schemas[schema_id] = schema
        return schema

    async def _decode(self, consumer_event: Any) -> PubSubEvent:
        event = consumer_event.event
        replay_id = bytes(consumer_event.replay_id)
        event_id: str | None = event.id or None
        schema = await self._schema(event.schema_id)
        try:
            payload = ps.decode_payload(schema, bytes(event.payload))
            change = ps.change_header(payload, schema.json)
        except UnexpectedResponse as exc:
            exc.event_id, exc.replay_id, exc.path = event_id, replay_id, self.topic
            raise
        return PubSubEvent(self.topic, replay_id, event_id, schema.schema_id, payload, change)

    # --- failures --------------------------------------------------------------------

    async def _recover(self, failure: ps.RpcFailure, *, idle: bool) -> None:
        """Prepare a resubscribe after ``failure``, or raise its typed error (§9.16.5)."""
        verdict = ps.classify_failure(failure, self.topic, idle=idle)
        error = verdict.error
        self._drop_stream()
        policy = self._t.retry
        delay = 0.0
        if verdict.action == "raise":
            raise error from None
        if verdict.action == "refresh":
            if self._auth_retried or self._token is None:
                raise error from None  # SessionExpired: the refreshed token was rejected too
            self._auth_retried = True
            await self._runtime.refresh(self._token, entered=self._generation)
        elif verdict.action == "invalid_replay":
            rejected = self._last_start[1]
            cast("ReplayUnavailable", error).replay_id = rejected
            if self._on_invalid == "raise" or rejected is None or self._fallback_used:
                raise error from None
            self._fallback_used = True
            preset: ps.Preset = "EARLIEST" if self._on_invalid == "earliest" else "LATEST"
            self._start = (preset, None, "none")
            self._tracker.position = None
        elif verdict.action == "retry_once":
            if self._license_retried:
                raise error from None
            self._license_retried = True
            delay = backoff_delay(policy, 1, self._t.rng)
        elif verdict.reason != "idle":
            self._attempts += 1
            if self._attempts >= policy.max_attempts:
                raise error from None
            delay = max(backoff_delay(policy, self._attempts, self._t.rng), verdict.min_delay)
        self._reconnects += 1
        self._t.emit(
            "sf.pubsub.reconnect",
            {
                "topic": self.topic,
                "attempt": self._attempts,
                "reason": verdict.reason,
                "error_code": error.error_code,
                "rpc_id": error.request_id,
                "delay_ms": round(delay * 1000, 1),
            },
        )
        if delay:
            await self._t.sleep(delay)
