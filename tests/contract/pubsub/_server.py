"""An in-process Pub/Sub API server for contract tests (spec §15, "Pub/Sub contract").

It implements ``Subscribe`` and ``GetSchema`` of the vendored ``pubsub_api.proto`` on a real
gRPC server bound to ``127.0.0.1``, so both clients run their actual gRPC code. respx can't mock
gRPC.

Each ``Subscribe`` stream runs the next *script* from :attr:`FakePubSubServer.scripts`: a
function taking a :class:`Stream` and returning an iterator of ``FetchResponse`` messages. A
script ends the stream cleanly by returning, or fails it with :meth:`Stream.abort`. With no
script left, the stream stays open (sending nothing) until the client cancels it.

Every response here is **synthetic**: the shapes come from the proto and from the Pub/Sub API
Developer Guide (*Subscribe RPC*, *Pull Subscription and Flow Control*, *Handle Errors*,
*Event Deserialization Considerations*), not from a sandbox recording (V27/V28).
"""

from __future__ import annotations

import io
import json
import queue
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import fastavro
import grpc

from even_simpler_salesforce._pubsub import pubsub_api_pb2 as pb
from even_simpler_salesforce._pubsub import pubsub_api_pb2_grpc as pb_grpc

CODE = "sfdc.platform.eventbus.grpc."

# --- schemas ---------------------------------------------------------------------------------

ORDER_SCHEMA_ID = "schema-order-1"
ORDER_SCHEMA: dict[str, Any] = {
    "type": "record",
    "name": "Order_Event__e",
    "namespace": "com.sforce.eventbus",
    "fields": [
        {"name": "CreatedDate", "type": "long", "doc": "CreatedDate:DateTime"},
        {"name": "CreatedById", "type": "string", "doc": "CreatedBy:EntityId"},
        {
            "name": "Order_Number__c",
            "type": ["null", "string"],
            "doc": "Data:Text:00Nxx0000000001",
            "default": None,
        },
        {"name": "Quantity__c", "type": ["null", "long"], "default": None},
    ],
}

ADDRESS: dict[str, Any] = {
    "type": "record",
    "name": "Address",
    "fields": [
        {"name": "Street", "type": ["null", "string"], "default": None},
        {"name": "City", "type": ["null", "string"], "default": None},
        {"name": "State", "type": ["null", "string"], "default": None},
        {"name": "PostalCode", "type": ["null", "string"], "default": None},
        {"name": "Country", "type": ["null", "string"], "default": None},
    ],
}

HEADER: dict[str, Any] = {
    "type": "record",
    "name": "ChangeEventHeader",
    "fields": [
        {"name": "entityName", "type": "string"},
        {"name": "recordIds", "type": {"type": "array", "items": "string"}},
        {"name": "changeType", "type": "string"},
        {"name": "changeOrigin", "type": "string"},
        {"name": "transactionKey", "type": "string"},
        {"name": "sequenceNumber", "type": "int"},
        {"name": "commitTimestamp", "type": "long"},
        {"name": "commitNumber", "type": "long"},
        {"name": "commitUser", "type": "string"},
        {"name": "nulledFields", "type": {"type": "array", "items": "string"}},
        {"name": "diffFields", "type": {"type": "array", "items": "string"}},
        {"name": "changedFields", "type": {"type": "array", "items": "string"}},
    ],
}

# Field positions matter: bit i of a bitmap is top-level field i.
#  0 ChangeEventHeader, 1 Name, 2 Description, 3 BillingAddress, 4 Phone, 5 LastModifiedDate
ACCOUNT_SCHEMA_ID = "schema-account-1"
ACCOUNT_SCHEMA: dict[str, Any] = {
    "type": "record",
    "name": "AccountChangeEvent",
    "namespace": "com.sforce.eventbus",
    "fields": [
        {"name": "ChangeEventHeader", "type": HEADER},
        {"name": "Name", "type": ["null", "string"], "default": None},
        {"name": "Description", "type": ["null", "string"], "default": None},
        {"name": "BillingAddress", "type": ["null", ADDRESS], "default": None},
        {"name": "Phone", "type": ["null", "string"], "default": None},
        {
            "name": "LastModifiedDate",
            "type": ["null", "long"],
            "doc": "LastModifiedDate:DateTime",
            "default": None,
        },
    ],
}

SCHEMAS = {ORDER_SCHEMA_ID: ORDER_SCHEMA, ACCOUNT_SCHEMA_ID: ACCOUNT_SCHEMA}
_PARSED = {k: fastavro.parse_schema(v) for k, v in SCHEMAS.items()}


def avro(schema_id: str, record: Mapping[str, Any]) -> bytes:
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, _PARSED[schema_id], dict(record))
    return buf.getvalue()


def rid(n: int) -> bytes:
    """A replay id (opaque bytes on the wire)."""
    return n.to_bytes(8, "big")


def order(n: int, *, created_ms: int = 1_767_225_600_000) -> pb.ConsumerEvent:
    """A platform event with replay id ``rid(n)`` and ``EventUuid`` ``uuid-{n}``."""
    payload = avro(
        ORDER_SCHEMA_ID,
        {
            "CreatedDate": created_ms,
            "CreatedById": "005xx0000000001AAA",
            "Order_Number__c": f"O-{n}",
            "Quantity__c": n,
        },
    )
    return pb.ConsumerEvent(
        event=pb.ProducerEvent(id=f"uuid-{n}", schema_id=ORDER_SCHEMA_ID, payload=payload),
        replay_id=rid(n),
    )


def account_change(
    n: int,
    change_type: str = "UPDATE",
    *,
    record_ids: Sequence[str] = ("001xx0000000001AAA",),
    changed: Sequence[str] = ("0x12",),
    nulled: Sequence[str] = (),
    diff: Sequence[str] = (),
    values: Mapping[str, Any] | None = None,
) -> pb.ConsumerEvent:
    record: dict[str, Any] = {
        "ChangeEventHeader": {
            "entityName": "Account",
            "recordIds": list(record_ids),
            "changeType": change_type,
            "changeOrigin": "com/salesforce/api/rest/68.0",
            "transactionKey": f"tx-{n}",
            "sequenceNumber": 1,
            "commitTimestamp": 1_767_225_600_000,
            "commitNumber": 1000 + n,
            "commitUser": "005xx0000000001AAA",
            "nulledFields": list(nulled),
            "diffFields": list(diff),
            "changedFields": list(changed),
        },
        "Name": "Acme",
        "Description": None,
        "BillingAddress": None,
        "Phone": "555-0100",
        "LastModifiedDate": 1_767_225_600_000,
        **(values or {}),
    }
    return pb.ConsumerEvent(
        event=pb.ProducerEvent(id="", schema_id=ACCOUNT_SCHEMA_ID, payload=avro(ACCOUNT_SCHEMA_ID, record)),
        replay_id=rid(n),
    )


def batch(*events: pb.ConsumerEvent, latest: bytes | None = None) -> pb.FetchResponse:
    return pb.FetchResponse(
        events=list(events),
        latest_replay_id=latest if latest is not None else (events[-1].replay_id if events else b""),
        rpc_id="rpc-batch",
    )


def keepalive(latest: bytes) -> pb.FetchResponse:
    return pb.FetchResponse(latest_replay_id=latest, rpc_id="rpc-keepalive")


# --- server ----------------------------------------------------------------------------------


class _Aborted(Exception):
    pass


@dataclass
class Stream:
    """One ``Subscribe`` call as the server sees it."""

    context: Any
    metadata: dict[str, str]
    requests: list[pb.FetchRequest] = field(default_factory=list)
    delivered: int = 0
    max_outstanding: int = 0
    _inbox: queue.SimpleQueue[pb.FetchRequest | None] = field(default_factory=queue.SimpleQueue)
    _requested: int = 0

    @property
    def first(self) -> pb.FetchRequest:
        return self.requests[0]

    def _received(self, req: pb.FetchRequest) -> None:
        self.requests.append(req)
        self._requested += req.num_requested
        self.max_outstanding = max(self.max_outstanding, self._requested - self.delivered)
        self._inbox.put(req)

    def next_request(self, timeout: float = 10.0) -> pb.FetchRequest:
        """Wait for the next ``FetchRequest`` from the client."""
        req = self._inbox.get(timeout=timeout)
        if req is None:
            raise _Aborted
        return req

    def send(self, response: pb.FetchResponse) -> pb.FetchResponse:
        self.delivered += len(response.events)
        return response

    def abort(self, status: grpc.StatusCode, details: str = "", error_code: str | None = None) -> None:
        trailers: list[tuple[str, str]] = [("rpc-id", "rpc-fail-1")]
        if error_code is not None:
            trailers.append(
                ("error-code", error_code if error_code.startswith("sfdc.") else CODE + error_code)
            )
        self.context.set_trailing_metadata(tuple(trailers))
        self.context.abort(status, details)


Script = Callable[[Stream], Iterator[pb.FetchResponse]]


class FakePubSubServer(pb_grpc.PubSubServicer):
    """Serves scripted streams on ``127.0.0.1:<port>``."""

    def __init__(self) -> None:
        self.scripts: list[Script] = []
        self.streams: list[Stream] = []
        self.schema_calls: list[tuple[str, dict[str, str]]] = []
        self.schemas: dict[str, str] = {k: json.dumps(v) for k, v in SCHEMAS.items()}
        self._lock = threading.Lock()
        self._server = grpc.server(ThreadPoolExecutor(max_workers=16))
        pb_grpc.add_PubSubServicer_to_server(self, self._server)
        port = self._server.add_insecure_port("127.0.0.1:0")
        self.endpoint = f"127.0.0.1:{port}"
        self._server.start()

    def stop(self) -> None:
        self._server.stop(grace=None)

    def script(self, *scripts: Script) -> None:
        self.scripts.extend(scripts)

    # --- servicer --------------------------------------------------------------------

    def GetSchema(self, request: pb.SchemaRequest, context: Any) -> pb.SchemaInfo:  # noqa: N802
        self.schema_calls.append((request.schema_id, dict(context.invocation_metadata())))
        schema = self.schemas.get(request.schema_id)
        if schema is None:
            context.set_trailing_metadata((("error-code", CODE + "schema.not.found"),))
            context.abort(grpc.StatusCode.NOT_FOUND, "no such schema")
        return pb.SchemaInfo(schema_json=schema, schema_id=request.schema_id, rpc_id="rpc-schema")

    def Subscribe(
        self, request_iterator: Iterator[pb.FetchRequest], context: Any
    ) -> Iterator[pb.FetchResponse]:  # noqa: N802
        stream = Stream(context, {k: v for k, v in context.invocation_metadata() if isinstance(v, str)})
        with self._lock:
            self.streams.append(stream)
            script = self.scripts.pop(0) if self.scripts else _hold

        def pump() -> None:
            try:
                for req in request_iterator:
                    stream._received(req)
            except Exception:
                pass
            stream._inbox.put(None)

        threading.Thread(target=pump, daemon=True).start()
        try:
            stream.next_request()  # the first request opens the subscription
            for response in script(stream):
                yield stream.send(response)
        except (_Aborted, queue.Empty):
            return


def _hold(stream: Stream) -> Iterator[pb.FetchResponse]:
    """Send nothing; end when the client cancels."""
    while True:
        stream.next_request(timeout=60)
    yield  # pragma: no cover


# --- ready-made scripts ------------------------------------------------------------------------


def sends(*responses: pb.FetchResponse, then: Callable[[Stream], None] | None = None) -> Script:
    """Send ``responses`` one by one, each after the client's next request has arrived if the
    previous one used up what was requested; then run ``then`` (e.g. an abort) or hold open."""

    def script(s: Stream) -> Iterator[pb.FetchResponse]:
        for r in responses:
            while s._requested - s.delivered < len(r.events):
                s.next_request()
            yield r
        if then is not None:
            then(s)
            return
        yield from _hold(s)

    return script


def fails(status: grpc.StatusCode, error_code: str | None = None, details: str = "failed") -> Script:
    def script(s: Stream) -> Iterator[pb.FetchResponse]:
        s.abort(status, details, error_code)
        yield from ()

    return script


def ends() -> Script:
    """End the stream cleanly right away (status OK)."""

    def script(s: Stream) -> Iterator[pb.FetchResponse]:
        yield from ()

    return script
