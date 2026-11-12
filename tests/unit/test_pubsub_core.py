"""Pure Pub/Sub decisions (``core/pubsub.py``, §9.16): arguments, flow control, replay rules,
failure classification, Avro and bitmap decoding.

Bitmap rules from *Event Deserialization Considerations* (Pub/Sub API Developer Guide, read
2026-11-09): https://developer.salesforce.com/docs/platform/pub-sub-api/guide/event-deserialization-considerations.html
Bit order (bit 0 = least-significant bit) is confirmed by the recorded CDC events replayed in
``tests/contract/pubsub/test_recorded.py`` (V27).
The schemas below are **synthetic**.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone
from typing import Any

import pytest

fastavro = pytest.importorskip("fastavro")

import even_simpler_salesforce as esf  # noqa: E402
from even_simpler_salesforce.core import pubsub as ps  # noqa: E402

ADDRESS = {
    "type": "record",
    "name": "Address",
    "fields": [{"name": n, "type": ["null", "string"], "default": None} for n in ("Street", "City", "State")],
}
SCHEMA: dict[str, Any] = {
    "type": "record",
    "name": "AccountChangeEvent",
    "fields": [
        {"name": "ChangeEventHeader", "type": {"type": "record", "name": "H", "fields": []}},
        {"name": "Name", "type": ["null", "string"], "default": None},
        {"name": "BillingAddress", "type": ["null", ADDRESS], "default": None},
        {"name": "Phone", "type": ["null", "string"], "default": None},
    ],
}


# --- arguments -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "match"),
    [
        (("Order__e", "latest", "raise", 1), "topic"),
        ((None, "latest", "raise", 1), "topic"),
        (("/event/A b", "latest", "raise", 1), "topic"),
        (("/event/A__e", "newest", "raise", 1), "replay"),
        (("/event/A__e", b"", "raise", 1), "replay"),
        (("/event/A__e", "latest", "skip", 1), "on_invalid_replay"),
        (("/event/A__e", "latest", "raise", 0), "batch_size"),
        (("/event/A__e", "latest", "raise", 101), "batch_size"),
        (("/event/A__e", "latest", "raise", True), "batch_size"),
        (("/event/A__e", "latest", "raise", 1.5), "batch_size"),
    ],
)
def test_check_subscribe_rejects(args: tuple[Any, ...], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ps.check_subscribe(*args)


@pytest.mark.parametrize("topic", ["/event/Order_Event__e", "/data/AccountChangeEvent", "/data/ChangeEvents"])
def test_check_subscribe_accepts(topic: str) -> None:
    ps.check_subscribe(topic, b"\x01", "earliest", 100)


@pytest.mark.parametrize(
    "endpoint", ["api.pubsub.salesforce.com:443", "api.deu.pubsub.salesforce.com:7443", "[::1]:50051"]
)
def test_check_endpoint_accepts(endpoint: str) -> None:
    assert ps.check_endpoint(endpoint) == endpoint


@pytest.mark.parametrize("endpoint", ["api.pubsub.salesforce.com", "https://x:443", "", 443])
def test_check_endpoint_rejects(endpoint: Any) -> None:
    with pytest.raises(ValueError, match="pubsub_endpoint"):
        ps.check_endpoint(endpoint)


def test_client_validates_the_endpoint() -> None:
    with pytest.raises(ValueError, match="pubsub_endpoint"):
        esf.Salesforce(esf.StaticToken("x", "https://acme.my.salesforce.com"), pubsub_endpoint="nope")


def test_replay_key_and_hex() -> None:
    assert ps.replay_key("00D1", "/event/A__e") == "00D1:/event/A__e"
    assert ps.replay_hex(b"\x00\xff") == "00ff"
    assert ps.replay_hex(None) is None


def test_start_position() -> None:
    assert ps.start_position(b"\x05", "earliest") == ("CUSTOM", b"\x05", "store")
    assert ps.start_position(None, b"\x07") == ("CUSTOM", b"\x07", "none")
    assert ps.start_position(None, "earliest") == ("EARLIEST", None, "none")
    assert ps.start_position(b"", "latest") == ("LATEST", None, "none")


def test_missing_extra() -> None:
    err = ps.missing_extra(ImportError("x", name="grpc"))
    assert "[pubsub] extra" in str(err)
    assert "grpc" in str(err)
    assert isinstance(err.__cause__, ImportError)
    assert "boom" in str(ps.missing_extra(ImportError("boom")))


# --- flow control ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pending", "buffered", "expected"),
    [(0, 0, 100), (50, 0, 50), (51, 0, 0), (20, 20, 60), (100, 0, 0), (0, 51, 0)],
)
def test_refill(pending: int, buffered: int, expected: int) -> None:
    assert ps.refill(100, pending, buffered) == expected


def test_refill_small_batches() -> None:
    assert ps.refill(1, 0, 0) == 1
    assert ps.refill(1, 1, 0) == 0
    assert ps.refill(3, 1, 0) == 2


# --- replay tracker --------------------------------------------------------------------------


def test_tracker_saves_at_batch_ends_and_on_finish() -> None:
    t = ps.ReplayTracker()
    assert t.finish() is None
    assert t.processed(b"1", end_of_batch=False) is None
    assert t.processed(b"2", end_of_batch=True) == b"2"
    assert t.processed(b"3", end_of_batch=False) is None
    assert t.position == b"3"
    assert t.finish() == b"3"
    assert t.finish() is None  # already saved


def test_tracker_keepalive_only_when_drained() -> None:
    t = ps.ReplayTracker()
    assert t.keepalive(b"9", drained=False) is None
    assert t.position is None
    assert t.keepalive(b"", drained=True) is None
    assert t.keepalive(b"9", drained=True) == b"9"
    assert t.keepalive(b"9", drained=True) is None


# --- failure classification ------------------------------------------------------------------


def fail(status: str, code: str | None = None, details: str = "x", **trailers: str) -> ps.RpcFailure:
    t = dict(trailers)
    if code is not None:
        t["error-code"] = code if code.startswith("sfdc.") else f"sfdc.platform.eventbus.grpc.{code}"
    return ps.RpcFailure(status, details, t)


@pytest.mark.parametrize(
    ("status", "code", "action", "cls", "reason"),
    [
        ("UNAUTHENTICATED", "service.auth.error", "refresh", esf.SessionExpired, "auth"),
        ("UNAUTHENTICATED", "auth.headers.invalid", "refresh", esf.SessionExpired, "auth"),
        ("UNAUTHENTICATED", None, "refresh", esf.SessionExpired, "auth"),
        (
            "INVALID_ARGUMENT",
            "subscription.fetch.replayid.corrupted",
            "invalid_replay",
            esf.ReplayUnavailable,
            "invalid_replay",
        ),
        ("NOT_FOUND", "topic.not.found", "raise", esf.NotFound, "error"),
        ("NOT_FOUND", None, "raise", esf.NotFound, "error"),
        ("PERMISSION_DENIED", "topic.cannot.subscribe", "raise", esf.Forbidden, "error"),
        ("PERMISSION_DENIED", "tenant.license", "retry_once", esf.Forbidden, "error"),
        ("PERMISSION_DENIED", None, "raise", esf.Forbidden, "error"),
        ("RESOURCE_EXHAUSTED", "concurrent.client.limit.exceeded", "raise", esf.PubSubLimitExceeded, "error"),
        ("RESOURCE_EXHAUSTED", "service.protection.rate", "raise", esf.PubSubLimitExceeded, "error"),
        ("RESOURCE_EXHAUSTED", "x.service.protection.rate", "raise", esf.PubSubLimitExceeded, "error"),
        ("RESOURCE_EXHAUSTED", None, "raise", esf.PubSubLimitExceeded, "error"),
        ("UNAVAILABLE", "service.unavailable", "reconnect", esf.PubSubError, "error"),
        ("INTERNAL", "subscription.internal.error", "reconnect", esf.PubSubError, "error"),
        ("INTERNAL", "replay.repeated", "reconnect", esf.PubSubError, "error"),
        ("UNAVAILABLE", "org.migrated", "reconnect", esf.PubSubError, "org_migrated"),
        ("UNAVAILABLE", None, "reconnect", esf.PubSubError, "error"),
        ("INTERNAL", None, "reconnect", esf.PubSubError, "error"),
        ("OK", None, "reconnect", esf.PubSubError, "error"),
        ("FAILED_PRECONDITION", "something.new", "raise", esf.PubSubError, "error"),
        ("UNKNOWN", None, "raise", esf.PubSubError, "error"),
    ],
)
def test_classify(
    status: str, code: str | None, action: str, cls: type[esf.SalesforceError], reason: str
) -> None:
    v = ps.classify_failure(fail(status, code, **{"rpc-id": "rpc-1"}), "/event/A__e")
    assert v.action == action
    assert type(v.error) is cls
    assert v.reason == reason
    assert v.error.request_id == "rpc-1"
    assert v.error.operation == "pubsub.subscribe"
    assert v.error.path == "/event/A__e"
    assert v.error.status is None
    assert v.error.error_code == (f"sfdc.platform.eventbus.grpc.{code}" if code else status)
    assert v.error.retryable is (action in ("reconnect", "refresh"))
    assert v.min_delay == (1800.0 if reason == "org_migrated" else 0.0)


def test_classify_messages_and_rpc_id_spelling() -> None:
    v = ps.classify_failure(ps.RpcFailure("OK", "", {"rpcid": "r2"}), "/event/A__e")
    assert v.error.message == "the server ended the stream"
    assert v.error.request_id == "r2"
    v = ps.classify_failure(ps.RpcFailure("UNKNOWN"), "/event/A__e")
    assert v.error.message == "gRPC status UNKNOWN"
    assert v.error.request_id is None
    assert str(ps.RpcFailure("UNAVAILABLE", "gone")) == "UNAVAILABLE: gone"
    assert str(ps.RpcFailure("UNAVAILABLE")) == "UNAVAILABLE"


@pytest.mark.parametrize(
    ("status", "code", "idle_reason"),
    [
        ("UNAVAILABLE", None, "idle"),
        ("OK", None, "idle"),
        ("CANCELLED", None, "idle"),
        ("DEADLINE_EXCEEDED", None, "idle"),
        ("UNAVAILABLE", "service.unavailable", "idle"),
        ("UNAVAILABLE", "org.migrated", "org_migrated"),  # never shortened
        ("UNAUTHENTICATED", None, "auth"),  # auth failures stay auth failures
        ("NOT_FOUND", "topic.not.found", "error"),
        ("FAILED_PRECONDITION", None, "error"),
    ],
)
def test_classify_idle(status: str, code: str | None, idle_reason: str) -> None:
    v = ps.classify_failure(fail(status, code), "/event/A__e", idle=True)
    assert v.reason == idle_reason


# --- schemas and payloads ----------------------------------------------------------------------


def encode(schema: dict[str, Any], record: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, fastavro.parse_schema(schema), record)
    return buf.getvalue()


PE_SCHEMA: dict[str, Any] = {
    "type": "record",
    "name": "Order_Event__e",
    "fields": [
        {"name": "CreatedDate", "type": "long", "doc": "CreatedDate:DateTime"},
        {"name": "Count__c", "type": ["null", "long"], "default": None, "doc": "Data:Number:00N"},
        {"name": "When__c", "type": ["null", "long"], "default": None, "doc": "Data:DateTime:00N"},
        {"name": "Day__c", "type": ["null", {"type": "int", "logicalType": "date"}], "default": None},
        {"name": "Due__c", "type": ["null", "long"], "default": None, "doc": "Data:DateOnly:00N"},
        {"name": "Plain__c", "type": ["null", "long"], "default": None, "doc": "Data:DateOnlyish"},
        {
            "name": "Meta",
            "type": [
                "null",
                {
                    "type": "record",
                    "name": "M",
                    "fields": [
                        {"name": "At", "type": ["null", "long"], "default": None, "doc": "At:DateTime"},
                    ],
                },
            ],
            "default": None,
        },
    ],
}


def test_parse_schema_and_datetime_paths() -> None:
    schema = ps.parse_schema("s1", json.dumps(PE_SCHEMA), json.loads)
    assert schema.schema_id == "s1"
    assert schema.field_names == (
        "CreatedDate",
        "Count__c",
        "When__c",
        "Day__c",
        "Due__c",
        "Plain__c",
        "Meta",
    )
    assert schema.datetimes == (("CreatedDate",), ("When__c",), ("Meta", "At"))
    assert schema.dates == (("Due__c",),)


@pytest.mark.parametrize("text", ["not json", "[]", '{"type": "record"}', '{"type": "nope", "fields": []}'])
def test_parse_schema_rejects(text: str) -> None:
    with pytest.raises(esf.UnexpectedResponse, match="s1"):
        ps.parse_schema("s1", text, json.loads)


def test_decode_payload_converts_marked_datetimes_only() -> None:
    schema = ps.parse_schema("s1", json.dumps(PE_SCHEMA), json.loads)
    record = {
        "CreatedDate": 1_767_225_600_000,
        "Count__c": 1_767_225_600_000,
        "When__c": None,
        "Day__c": 20454,
        "Due__c": 1_794_441_600_000,  # recorded: a Date is epoch ms at midnight UTC
        "Plain__c": 1_794_441_600_000,
        "Meta": {"At": 0},
    }
    out = ps.decode_payload(schema, encode(PE_SCHEMA, record))
    assert out["CreatedDate"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert out["Count__c"] == 1_767_225_600_000
    assert out["When__c"] is None
    assert out["Day__c"].isoformat() == "2026-01-01"
    assert out["Due__c"] == date(2026, 11, 12)
    assert out["Plain__c"] == 1_794_441_600_000
    assert out["Meta"]["At"] == datetime(1970, 1, 1, tzinfo=timezone.utc)

    out = ps.decode_payload(schema, encode(PE_SCHEMA, {**record, "Meta": None}))
    assert out["Meta"] is None


def test_decode_payload_rejects_garbage_and_trailing_bytes() -> None:
    schema = ps.parse_schema("s1", json.dumps(PE_SCHEMA), json.loads)
    with pytest.raises(esf.UnexpectedResponse, match="doesn't decode"):
        ps.decode_payload(schema, b"\xff")
    good = encode(PE_SCHEMA, {"CreatedDate": 1})
    with pytest.raises(esf.UnexpectedResponse, match="trailing bytes"):
        ps.decode_payload(schema, good + b"\x00")


# --- bitmaps ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entries", "names"),
    [
        ([], ()),
        (["0x0"], ()),
        (["0x2"], ("Name",)),
        (["0xA"], ("Name", "Phone")),
        (["0xa"], ("Name", "Phone")),
        (["2-0x2"], ("BillingAddress.City",)),  # only compound fields nulled: no leading 0x entry
        (["0x8", "2-0x5"], ("Phone", "BillingAddress.Street", "BillingAddress.State")),
    ],
)
def test_decode_bitmap(entries: list[str], names: tuple[str, ...]) -> None:
    assert ps.decode_bitmap(entries, SCHEMA) == names


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        (["0x10"], "bit 4 but the schema has 4 fields"),
        (["9-0x1"], "compound position 9"),
        (["1-0x1"], "'Name' at position 1 is not a compound field"),
        (["2-0x8"], "bit 3 of 'BillingAddress'"),
        (["zz"], "unknown entry"),
        (["0x"], "unknown entry"),
    ],
)
def test_decode_bitmap_rejects(entries: list[str], match: str) -> None:
    with pytest.raises(esf.UnexpectedResponse, match=match):
        ps.decode_bitmap(entries, SCHEMA)


def test_decode_bitmap_wide_schema() -> None:
    wide = {"fields": [{"name": f"F{i}"} for i in range(70)]}
    assert ps.decode_bitmap([hex(1 << 69 | 1)], wide) == ("F0", "F69")


# --- change header ---------------------------------------------------------------------------


def header(**kw: Any) -> dict[str, Any]:
    h: dict[str, Any] = {
        "entityName": "Account",
        "recordIds": ["001A"],
        "changeType": "UPDATE",
        "changeOrigin": "",
        "transactionKey": "tx",
        "sequenceNumber": 2,
        "commitTimestamp": 1_767_225_600_000,
        "commitNumber": 7,
        "commitUser": "005A",
        "changedFields": ["0x2"],
        "nulledFields": [],
        "diffFields": None,
    }
    h.update(kw)
    return {"ChangeEventHeader": h}


def test_change_header() -> None:
    ch = ps.change_header(header(), SCHEMA)
    assert ch == esf.ChangeHeader(
        entity_name="Account",
        change_type=esf.ChangeType.UPDATE,
        record_ids=("001A",),
        changed_fields=("Name",),
        transaction_key="tx",
        sequence_number=2,
        commit_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        commit_user="005A",
        commit_number=7,
        change_origin=None,
    )


def test_change_header_odd_values() -> None:
    ch = ps.change_header(
        header(
            recordIds=["000000000000000AAA", ""],
            changeType="GAP_OVERFLOW",
            sequenceNumber="2",
            commitNumber=None,
            commitTimestamp=datetime(2026, 1, 1),
            changedFields=None,
        ),
        SCHEMA,
    )
    assert ch is not None
    assert ch.record_ids == ()
    assert ch.is_overflow
    assert ch.is_gap
    assert ch.sequence_number is None
    assert ch.commit_number is None
    assert ch.commit_timestamp == datetime(2026, 1, 1, tzinfo=timezone.utc)
    ch = ps.change_header(header(commitTimestamp="soon"), SCHEMA)
    assert ch is not None
    assert ch.commit_timestamp is None
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ch = ps.change_header(header(commitTimestamp=aware), SCHEMA)
    assert ch is not None
    assert ch.commit_timestamp is aware


def test_platform_event_has_no_header() -> None:
    assert ps.change_header({"CreatedDate": 1}, SCHEMA) is None


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"changeType": "TELEPORT"}, "unknown CDC changeType 'TELEPORT'"),
        ({"entityName": ""}, "no entityName"),
        ({"recordIds": "001A"}, "not an array"),
    ],
)
def test_change_header_rejects(kw: dict[str, Any], match: str) -> None:
    with pytest.raises(esf.UnexpectedResponse, match=match):
        ps.change_header(header(**kw), SCHEMA)


@pytest.mark.parametrize("kind", list(esf.ChangeType))
def test_change_types(kind: esf.ChangeType) -> None:
    assert kind.is_gap is kind.value.startswith("GAP_")


def test_event_time() -> None:
    ch = ps.change_header(header(), SCHEMA)
    assert ps.event_time({}, ch) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ps.event_time({"CreatedDate": 0}, None) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert ps.event_time({"CreatedDate": True}, None) is None
    no_time = ps.change_header(header(commitTimestamp=None), SCHEMA)
    assert ps.event_time({}, no_time) is None


def test_missing_fastavro_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    schema = ps.parse_schema("s1", json.dumps(PE_SCHEMA), json.loads)
    monkeypatch.setitem(sys.modules, "fastavro", None)
    with pytest.raises(ImportError, match=r"\[pubsub\] extra"):
        ps.parse_schema("s1", json.dumps(PE_SCHEMA), json.loads)
    with pytest.raises(ImportError, match=r"\[pubsub\] extra"):
        ps.decode_payload(schema, b"")


@pytest.mark.parametrize("how", ["absent", "blocked"])
def test_subscribe_without_the_extra(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    import importlib.util
    import sys

    if how == "absent":
        real = importlib.util.find_spec
        monkeypatch.setattr(importlib.util, "find_spec", lambda n: None if n == "fastavro" else real(n))
    else:
        monkeypatch.setitem(sys.modules, "fastavro", None)
    for cls in (esf.Salesforce, esf.AsyncSalesforce):
        sf = cls(esf.StaticToken("x", "https://acme.my.salesforce.com"))
        with pytest.raises(ImportError, match=r"\[pubsub\] extra.*fastavro"):
            sf.pubsub.subscribe("/event/A__e")


@pytest.mark.parametrize("module", ["_async", "_sync"])
def test_grpc_helpers(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    import asyncio
    import importlib
    import sys

    compat = importlib.import_module(f"even_simpler_salesforce.{module}._compat")
    if module == "_async":  # grpc.aio channels need a running event loop

        async def open_and_close() -> None:
            await compat.grpc_close(compat.grpc_channel("api.pubsub.salesforce.com:443"))

        asyncio.run(open_and_close())
    else:
        compat.grpc_close(compat.grpc_channel("api.pubsub.salesforce.com:443"))  # TLS; connects lazily
    monkeypatch.setitem(sys.modules, "grpc", None)
    with pytest.raises(ImportError, match=r"\[pubsub\] extra"):
        compat.grpc_channel("127.0.0.1:1")
