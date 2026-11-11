"""Pub/Sub API decisions (§9.16, D31). No I/O.

Everything the gRPC transport decides lives here as pure functions, so both clients share it
and it is tested without a network:

- argument checks and the start position of a subscription (§9.16.4);
- flow control: how many events to request next (§9.16.2);
- replay bookkeeping: which replay id to save, and when (§9.16.4);
- Avro decoding of payloads and the ``ChangeEventHeader`` with its field bitmaps (§9.16.3);
- classification of gRPC failures into the §8 hierarchy (§9.16.5).

``fastavro`` is imported on first use, so this module imports without the ``[pubsub]`` extra.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, cast

from ..errors import (
    Forbidden,
    NotFound,
    PubSubError,
    PubSubLimitExceeded,
    ReplayUnavailable,
    SalesforceError,
    SessionExpired,
    UnexpectedResponse,
)
from ..models import ChangeHeader, ChangeType

__all__ = [
    "DEFAULT_ENDPOINT",
    "EMPTY_RECORD_ID",
    "MAX_BATCH_SIZE",
    "ORG_MIGRATED_DELAY",
    "OPERATION",
    "Replay",
    "ReplayTracker",
    "RpcFailure",
    "Schema",
    "Verdict",
    "change_header",
    "check_endpoint",
    "check_subscribe",
    "classify_failure",
    "datetime_paths",
    "decode_bitmap",
    "decode_payload",
    "event_time",
    "missing_extra",
    "parse_schema",
    "refill",
    "replay_hex",
    "replay_key",
    "start_position",
]

#: The global Pub/Sub API endpoint. ``api.deu.pubsub.salesforce.com:443`` keeps data in transit
#: in the EU; port 7443 is accepted too.
DEFAULT_ENDPOINT = "api.pubsub.salesforce.com:443"
#: Salesforce rejects ``num_requested`` above 100.
MAX_BATCH_SIZE = 100
#: The record id an overflow event carries instead of real ids.
EMPTY_RECORD_ID = "000000000000000AAA"
#: Salesforce asks clients to wait 30 minutes before resubscribing after an org migration.
ORG_MIGRATED_DELAY = 1800.0
OPERATION = "pubsub.subscribe"
_CODE_PREFIX = "sfdc.platform.eventbus.grpc."

Replay = Literal["latest", "earliest"] | bytes
Preset = Literal["LATEST", "EARLIEST", "CUSTOM"]
OnInvalidReplay = Literal["raise", "latest", "earliest"]


def missing_extra(exc: ImportError) -> ImportError:
    """The error to raise when a ``[pubsub]`` dependency can't be imported."""
    err = ImportError(
        "the Pub/Sub API needs the [pubsub] extra: pip install 'even-simpler-salesforce[pubsub]' "
        f"({exc.name or exc})"
    )
    err.__cause__ = exc
    return err


# --- arguments and positions ----------------------------------------------------------------


_TOPIC = re.compile(r"/(event|data)/[A-Za-z0-9_]+")


def check_subscribe(topic: Any, replay: Any, on_invalid_replay: Any, batch_size: Any) -> None:
    """Validate the arguments of ``sf.pubsub.subscribe()``.

    Raises:
        ValueError: a bad topic, replay, ``on_invalid_replay`` or ``batch_size``.
    """
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic):
        raise ValueError(
            f"topic must look like '/event/Name__e' or '/data/AccountChangeEvent', got {topic!r}"
        )
    if not (replay in ("latest", "earliest") or (isinstance(replay, bytes) and replay)):
        raise ValueError("replay must be 'latest', 'earliest' or a non-empty replay id (bytes)")
    if on_invalid_replay not in ("raise", "latest", "earliest"):
        raise ValueError("on_invalid_replay must be 'raise', 'latest' or 'earliest'")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_BATCH_SIZE
    ):
        raise ValueError(f"batch_size must be an int from 1 to {MAX_BATCH_SIZE} (the server's limit)")


_ENDPOINT = re.compile(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\]):\d{1,5}")


def check_endpoint(endpoint: str) -> str:
    """Validate a Pub/Sub ``host:port`` endpoint.

    Raises:
        ValueError: not a ``host:port`` string.
    """
    if not isinstance(endpoint, str) or not _ENDPOINT.fullmatch(endpoint):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"pubsub_endpoint must look like 'api.pubsub.salesforce.com:443', got {endpoint!r}")
    return endpoint


def replay_key(org_id: str, topic: str) -> str:
    """Replay-store key: per org, so a sandbox refresh never resumes another org's position."""
    return f"{org_id}:{topic}"


def replay_hex(replay_id: bytes | None) -> str | None:
    """A replay id for logs (an opaque position, not a secret)."""
    return replay_id.hex() if replay_id is not None else None


def start_position(stored: bytes | None, replay: Replay) -> tuple[Preset, bytes | None, str]:
    """``(replay_preset, replay_id, resumed_from)`` of the first stream.

    A replay id from the store wins over ``replay=``.
    """
    if stored:
        return "CUSTOM", stored, "store"
    if isinstance(replay, bytes):
        return "CUSTOM", replay, "none"
    return ("EARLIEST" if replay == "earliest" else "LATEST"), None, "none"


# --- flow control -----------------------------------------------------------------------------


def refill(batch_size: int, pending: int, buffered: int) -> int:
    """How many more events to request (``num_requested``), or 0.

    The client keeps at most ``batch_size`` events requested but not yet consumed. It asks for
    more once at most half of that is outstanding, so a fast consumer never waits for a round
    trip and a slow one never holds more than one batch.
    """
    outstanding = pending + buffered
    if outstanding > batch_size // 2:
        return 0
    return batch_size - outstanding


# --- replay bookkeeping -----------------------------------------------------------------------


@dataclass(slots=True)
class ReplayTracker:
    """Which replay id to resume from and which to save (at-least-once delivery, §9.16.4).

    ``position`` is the last processed replay id: every reconnect resumes after it, with or
    without a replay store. Saves happen at the end of each fully consumed batch, for a
    keepalive when nothing is outstanding, and on a normal exit.
    """

    position: bytes | None = None
    saved: bytes | None = None

    def processed(self, replay_id: bytes, *, end_of_batch: bool) -> bytes | None:
        """Record that the consumer finished an event; returns the id to save, if any."""
        self.position = replay_id
        return self._due() if end_of_batch else None

    def keepalive(self, latest_replay_id: bytes, *, drained: bool) -> bytes | None:
        """Handle the ``latest_replay_id`` of an empty response.

        It is only safe to move to that position when every delivered event was processed and
        nothing is buffered; saving it earlier would skip the buffered events.
        """
        if not drained or not latest_replay_id:
            return None
        self.position = latest_replay_id
        return self._due()

    def finish(self) -> bytes | None:
        """The id to save on a normal exit, if it was not saved yet."""
        return self._due()

    def _due(self) -> bytes | None:
        if self.position is None or self.position == self.saved:
            return None
        self.saved = self.position
        return self.position


# --- failures ---------------------------------------------------------------------------------


class RpcFailure(Exception):
    """A failed gRPC call, independent of the grpc package (raised by ``_compat.GrpcStream``).

    Attributes:
        status: gRPC status name, e.g. ``"UNAUTHENTICATED"``. ``"OK"`` when the server ended the
            stream without an error.
        details: Status details from the server.
        trailers: Trailing metadata (``error-code``, ``rpc-id``, ...), lower-case keys.
    """

    def __init__(self, status: str, details: str = "", trailers: Mapping[str, str] | None = None) -> None:
        super().__init__(f"{status}: {details}" if details else status)
        self.status = status
        self.details = details
        self.trailers = dict(trailers or {})


Action = Literal["refresh", "invalid_replay", "reconnect", "retry_once", "raise"]


@dataclass(frozen=True, slots=True)
class Verdict:
    """What to do about a :class:`RpcFailure`.

    Attributes:
        action: ``"refresh"`` the token and resubscribe; ``"invalid_replay"`` (apply
            ``on_invalid_replay``); ``"reconnect"`` with backoff; ``"retry_once"`` then raise;
            or ``"raise"`` :attr:`error` now.
        error: The typed error, raised when the client gives up.
        reason: The ``reason`` of the ``sf.pubsub.reconnect`` event.
        min_delay: The shortest wait before resubscribing, in seconds.
    """

    action: Action
    error: SalesforceError
    reason: str = "error"
    min_delay: float = 0.0


# (code suffix after "sfdc.platform.eventbus.grpc.", action, error class, reason)
_CODES: tuple[tuple[str, Action, type[SalesforceError], str], ...] = (
    ("service.auth.error", "refresh", SessionExpired, "auth"),
    ("auth.headers.invalid", "refresh", SessionExpired, "auth"),
    ("auth.refresh.invalid", "refresh", SessionExpired, "auth"),
    ("subscription.fetch.replayid.corrupted", "invalid_replay", ReplayUnavailable, "invalid_replay"),
    ("subscription.fetch.replayid.validation.failed", "invalid_replay", ReplayUnavailable, "invalid_replay"),
    ("topic.not.found", "raise", NotFound, "error"),
    ("topic.cannot.subscribe", "raise", Forbidden, "error"),
    ("topic.meta.permission", "raise", Forbidden, "error"),
    ("tenant.license", "retry_once", Forbidden, "error"),
    ("concurrent.client.limit.exceeded", "raise", PubSubLimitExceeded, "error"),
    ("subscription.limit.exceeded", "raise", PubSubLimitExceeded, "error"),
    ("service.unavailable", "reconnect", PubSubError, "error"),
    ("subscription.internal.error", "reconnect", PubSubError, "error"),
    ("replay.repeated", "reconnect", PubSubError, "error"),
    ("org.migrated", "reconnect", PubSubError, "org_migrated"),
)
_STATUS: dict[str, tuple[Action, type[SalesforceError]]] = {
    "UNAUTHENTICATED": ("refresh", SessionExpired),
    "NOT_FOUND": ("raise", NotFound),
    "PERMISSION_DENIED": ("raise", Forbidden),
    "RESOURCE_EXHAUSTED": ("raise", PubSubLimitExceeded),
    "UNAVAILABLE": ("reconnect", PubSubError),
    "OK": ("reconnect", PubSubError),  # the server ended the stream
}
_IDLE_STATUSES = frozenset({"OK", "CANCELLED", "UNAVAILABLE", "INTERNAL", "UNKNOWN", "DEADLINE_EXCEEDED"})


def _rule(code: str | None, status: str) -> tuple[Action, type[SalesforceError], str]:
    if code:
        suffix = code.removeprefix(_CODE_PREFIX)
        for known, action, cls, reason in _CODES:
            if suffix == known or suffix.endswith("." + known):
                return action, cls, reason
        if suffix.startswith("service.protection") or ".service.protection" in suffix:
            return "raise", PubSubLimitExceeded, "error"
    if status in _STATUS:
        action, cls = _STATUS[status]
        return action, cls, "auth" if action == "refresh" else "error"
    if status == "INTERNAL" and not code:  # e.g. "Received RST_STREAM"
        return "reconnect", PubSubError, "error"
    return "raise", PubSubError, "error"


def classify_failure(failure: RpcFailure, topic: str, *, idle: bool = False) -> Verdict:
    """Map a gRPC status and the Pub/Sub ``error-code`` trailer onto §8 and an action.

    Args:
        failure: The failed call.
        topic: The subscription's topic (becomes the error's ``path``).
        idle: The stream had no events requested while the consumer worked, so Salesforce
            may have closed it for inactivity (60 seconds without a ``FetchRequest``).
    """
    code = failure.trailers.get("error-code") or None
    rpc_id = failure.trailers.get("rpc-id") or failure.trailers.get("rpcid") or None
    action, cls, reason = _rule(code, failure.status)
    retryable = action in ("reconnect", "refresh")
    if (
        idle
        and reason == "error"  # never shortens the org.migrated wait or skips an auth refresh
        and action in ("reconnect", "raise")
        and failure.status in _IDLE_STATUSES
        and (action == "reconnect" or code is None)
    ):
        action, cls, reason, retryable = "reconnect", PubSubError, "idle", True
    message = failure.details or (
        "the server ended the stream" if failure.status == "OK" else f"gRPC status {failure.status}"
    )
    error = cls(
        message,
        error_code=code or failure.status,
        request_id=rpc_id,
        operation=OPERATION,
        path=topic,
        retryable=retryable,
    )
    return Verdict(action, error, reason, ORG_MIGRATED_DELAY if reason == "org_migrated" else 0.0)


# --- decoding -------------------------------------------------------------------------------


def _fastavro() -> Any:
    try:
        import fastavro
    except ImportError as exc:
        raise missing_extra(exc) from None
    return fastavro


@dataclass(frozen=True, slots=True)
class Schema:
    """A parsed Avro schema: the JSON as a dict, plus fastavro's parsed form.

    ``datetimes`` lists the paths of the fields the schema marks as DateTime (see
    :func:`datetime_paths`).
    """

    schema_id: str
    json: Mapping[str, Any]
    parsed: Any = field(repr=False)
    datetimes: tuple[tuple[str, ...], ...] = ()

    @property
    def field_names(self) -> tuple[str, ...]:
        """Top-level field names, in schema order (bit *i* of a bitmap is field *i*)."""
        return tuple(str(f["name"]) for f in self.json.get("fields", ()))


def parse_schema(schema_id: str, schema_json: str, loads: Callable[[str], Any]) -> Schema:
    """Parse the ``schema_json`` returned by ``GetSchema``.

    Raises:
        UnexpectedResponse: the schema is not a valid Avro record schema.
    """
    try:
        data = loads(schema_json)
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list):  # pyright: ignore[reportUnknownMemberType]
            raise ValueError("not an Avro record schema")
        parsed = _fastavro().parse_schema(data)
    except ImportError:
        raise
    except Exception as exc:
        raise UnexpectedResponse(
            f"Pub/Sub schema {schema_id} is not a valid Avro record schema: {exc}", operation=OPERATION
        ) from None
    schema = cast("dict[str, Any]", data)
    return Schema(schema_id, schema, parsed, datetime_paths(schema))


# Salesforce marks field types in the Avro ``doc`` attribute, e.g. ``"CreatedDate:DateTime"``
# or ``"Data:DateTime:00N..."`` (REST API Developer Guide, *Platform Event Schema by Event Name*).
_DATETIME_DOC = re.compile(r"^[^:]*:DateTime(:|$)")


def datetime_paths(schema: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Paths of the fields whose ``doc`` marks them as DateTime, also inside compound fields.

    Salesforce sends DateTime values as epoch milliseconds in a plain ``long``; the ``doc``
    attribute is the only place that says what they are (V28).
    """
    out: list[tuple[str, ...]] = []

    def walk(fields: list[Mapping[str, Any]], prefix: tuple[str, ...]) -> None:
        for f in fields:
            path = (*prefix, str(f["name"]))
            doc = f.get("doc")
            if isinstance(doc, str) and _DATETIME_DOC.match(doc):
                out.append(path)
            nested = _record_fields(f.get("type"))
            if nested:
                walk(nested, path)

    walk(list(schema.get("fields") or []), ())
    return tuple(out)


def _convert_datetimes(record: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> None:
    for path in paths:
        node: Any = record
        for name in path[:-1]:
            node = cast("dict[str, Any]", node).get(name) if isinstance(node, dict) else None
        if isinstance(node, dict):
            target = cast("dict[str, Any]", node)
            value = target.get(path[-1])
            converted = _to_datetime(value) if isinstance(value, int) else None
            if converted is not None:
                target[path[-1]] = converted


def decode_payload(schema: Schema, payload: bytes) -> dict[str, Any]:
    """Decode one Avro binary payload.

    A field becomes an aware UTC ``datetime`` when the schema marks it as DateTime (in its
    ``doc``, or with a ``timestamp-millis`` logical type), and a ``date`` with a ``date``
    logical type. Every other ``long`` stays an ``int``.

    Raises:
        UnexpectedResponse: the payload does not match the schema. Salesforce does not validate
            Avro on publish, so such events can exist.
    """
    fastavro = _fastavro()
    buf = io.BytesIO(payload)
    try:
        record = fastavro.schemaless_reader(buf, schema.parsed, None)
    except Exception as exc:
        raise UnexpectedResponse(
            f"event payload doesn't decode against schema {schema.schema_id}: {type(exc).__name__}",
            operation=OPERATION,
        ) from None
    if buf.tell() != len(payload) or not isinstance(record, dict):
        raise UnexpectedResponse(
            f"event payload doesn't decode against schema {schema.schema_id}: trailing bytes",
            operation=OPERATION,
        )
    result = cast("dict[str, Any]", record)
    _convert_datetimes(result, schema.datetimes)
    return result


_TOP = re.compile(r"0x([0-9A-Fa-f]+)")
_NESTED = re.compile(r"(\d+)-0x([0-9A-Fa-f]+)")


def _record_fields(avro_type: Any) -> list[Mapping[str, Any]] | None:
    """The fields of a record type, also inside a ``["null", record]`` union."""
    if isinstance(avro_type, list):
        for member in cast("list[Any]", avro_type):
            found = _record_fields(member)
            if found is not None:
                return found
        return None
    if isinstance(avro_type, dict) and avro_type.get("type") == "record":  # pyright: ignore[reportUnknownMemberType]
        return cast("list[Mapping[str, Any]]", avro_type.get("fields") or [])  # pyright: ignore[reportUnknownMemberType]
    return None


def _bits(hex_digits: str) -> list[int]:
    """Positions of the set bits; bit 0 is the least-significant bit of the hex value (V27)."""
    value = int(hex_digits, 16)
    return [i for i in range(value.bit_length()) if value >> i & 1]


def decode_bitmap(entries: Sequence[str], schema: Mapping[str, Any]) -> tuple[str, ...]:
    """Decode ``changedFields`` / ``nulledFields`` / ``diffFields`` into field names.

    Follows *Event Deserialization Considerations* (Pub/Sub API Developer Guide):

    - ``0x…`` is a bitmap over the top-level fields of the event schema; set bit *i* means
      field *i*.
    - ``{pos}-0x…`` is a bitmap over the fields of the compound field at top-level position
      ``pos``; those fields are named ``Parent.Child``.

    Raises:
        UnexpectedResponse: an entry is malformed or points outside the schema.
    """
    fields: list[Mapping[str, Any]] = list(schema.get("fields") or [])
    names: list[str] = []

    def bad(why: str) -> UnexpectedResponse:
        return UnexpectedResponse(f"can't decode field bitmap: {why}", operation=OPERATION)

    for entry in entries:
        if m := _TOP.fullmatch(entry):
            for i in _bits(m.group(1)):
                if i >= len(fields):
                    raise bad(f"bit {i} but the schema has {len(fields)} fields")
                names.append(str(fields[i]["name"]))
        elif m := _NESTED.fullmatch(entry):
            pos = int(m.group(1))
            if pos >= len(fields):
                raise bad(f"compound position {pos} but the schema has {len(fields)} fields")
            parent = fields[pos]
            nested = _record_fields(parent.get("type"))
            if nested is None:
                raise bad(f"field {parent['name']!r} at position {pos} is not a compound field")
            for i in _bits(m.group(2)):
                if i >= len(nested):
                    raise bad(f"bit {i} of {parent['name']!r} but it has {len(nested)} fields")
                names.append(f"{parent['name']}.{nested[i]['name']}")
        else:
            raise bad(f"unknown entry {entry!r}")
    return tuple(names)


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, int) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise UnexpectedResponse("ChangeEventHeader field is not an array", operation=OPERATION)
    return [str(v) for v in cast("list[Any]", value)]


def change_header(payload: Mapping[str, Any], schema: Mapping[str, Any]) -> ChangeHeader | None:
    """Build the :class:`ChangeHeader` of a change event; ``None`` for a platform event.

    Raises:
        UnexpectedResponse: an unknown ``changeType`` or a malformed header.
    """
    raw = payload.get("ChangeEventHeader")
    if not isinstance(raw, dict):
        return None
    header = cast("dict[str, Any]", raw)
    try:
        change_type = ChangeType(header.get("changeType"))
    except ValueError:
        raise UnexpectedResponse(
            f"unknown CDC changeType {header.get('changeType')!r}", operation=OPERATION
        ) from None
    entity = header.get("entityName")
    if not isinstance(entity, str) or not entity:
        raise UnexpectedResponse("ChangeEventHeader has no entityName", operation=OPERATION)
    seq = header.get("sequenceNumber")
    commit_number = header.get("commitNumber")
    return ChangeHeader(
        entity_name=entity,
        change_type=change_type,
        record_ids=tuple(r for r in _str_list(header.get("recordIds")) if r and r != EMPTY_RECORD_ID),
        changed_fields=decode_bitmap(_str_list(header.get("changedFields")), schema),
        nulled_fields=decode_bitmap(_str_list(header.get("nulledFields")), schema),
        diff_fields=decode_bitmap(_str_list(header.get("diffFields")), schema),
        transaction_key=_opt_str(header.get("transactionKey")),
        sequence_number=seq if isinstance(seq, int) else None,
        commit_timestamp=_to_datetime(header.get("commitTimestamp")),
        commit_user=_opt_str(header.get("commitUser")),
        commit_number=commit_number if isinstance(commit_number, int) else None,
        change_origin=_opt_str(header.get("changeOrigin")),
    )


def event_time(payload: Mapping[str, Any], change: ChangeHeader | None) -> datetime | None:
    """When the event happened: the commit time of a change event, else ``CreatedDate``."""
    if change is not None and change.commit_timestamp is not None:
        return change.commit_timestamp
    return _to_datetime(payload.get("CreatedDate"))
