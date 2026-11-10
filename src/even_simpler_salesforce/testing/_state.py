"""Shared in-memory state and behaviour of the fakes (the single source of truth).

``FakeSalesforce`` and ``AsyncFakeSalesforce`` are thin facades over :class:`FakeState`.
"""

from __future__ import annotations

import copy
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

from ..config import Budget, Priority
from ..context import current_call_context
from ..core.cursor import Cursor, decode_cursor, encode_cursor
from ..core.http import build_url, check_record_id, check_sobject, path_template
from ..core.limits import LimitInfo
from ..core.records import COLLECTION_WRITE_CHUNK, apply_model, chunked, format_sf_datetime, with_type
from ..core.soql import soql_hash, soql_identifier
from ..errors import (
    Conflict,
    ErrorDetail,
    InvalidField,
    MalformedQuery,
    MultipleMatches,
    NotFound,
    RequiredFieldMissing,
    SalesforceError,
)
from ..models import BatchResult, Limit, QueryPlan, QueryResult, RecordResult, UpsertResult
from ..observability.events import Observer, ObserverHub
from ..observability.summary import record_event
from ..usage import UsageTracker
from ._ids import key_prefix, make_id

__all__ = [
    "FakeCall",
    "FakeRequest",
    "FakeState",
    "QueryScript",
    "UnscriptedCall",
    "Who",
]

T = TypeVar("T")

Rows = Sequence[Mapping[str, Any]]
RowsSpec = Rows | Callable[[str], Rows]
Match = str | re.Pattern[str] | Callable[[str], bool]

_FROM = re.compile(r"\bFROM\s+([A-Za-z][A-Za-z0-9_]*)", re.IGNORECASE)
MAX_OPEN_LOCATORS = 1000
_LOCATOR = re.compile(r"/query(?:All)?/([0-9A-Za-z]{15,18})-(\d+)$")
_VERSIONED = re.compile(r"^/services/data/v\d+\.\d/")
DEFAULT_PAGE_SIZE = 2000


class UnscriptedCall(AssertionError):
    """The code under test made a call the fake has no script or handler for.

    Subclasses ``AssertionError`` on purpose: it signals a test-setup problem and is never
    swallowed by ``except esf.SalesforceError``.
    """


@dataclass(slots=True)
class FakeCall:
    """One call made against a fake client.

    Attributes:
        operation: Operation name, as used by the real client (``"sobject.update"``, ``"query"``).
        sobject: sObject type involved, if any.
        args: The call's arguments by name.
        caller: Caller tag (from ``tagged()`` or ``esf.call_context``).
        priority: Call priority.
        error: The exception the call raised, if any.
    """

    operation: str
    sobject: str | None
    args: Mapping[str, Any]
    caller: str | None = None
    priority: str = "normal"
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class FakeRequest:
    """What a ``fake.on_request(...)`` handler receives.

    Attributes:
        method: Upper-case HTTP method.
        path: Normalised path (relative to ``/services/data/vXX.X/`` when versioned).
        params: Query parameters.
        json: Decoded JSON body, if ``json=`` was passed.
        content: Raw body, if ``content=`` was passed.
        headers: Extra headers.
    """

    method: str
    path: str
    params: Mapping[str, Any]
    json: Any
    content: bytes | None
    headers: Mapping[str, str]


@dataclass(slots=True)
class QueryScript:
    """A scripted answer for ``query``/``count``/``search`` (returned by ``fake.on_query``).

    Attributes:
        match: SOQL text (whitespace-insensitive), compiled regex, or predicate.
        rows: Rows, or a callable receiving the SOQL and returning rows.
        total_size: ``totalSize`` to report (defaults to the number of rows).
        times: How many times the script may match (``None`` = unlimited).
        kind: ``"query"`` or ``"search"``.
        hits: How many times it matched so far.
    """

    match: Match
    rows: RowsSpec
    total_size: int | None = None
    times: int | None = None
    kind: str = "query"
    hits: int = 0

    def matches(self, text: str) -> bool:
        """Whether this script answers ``text``."""
        if self.times is not None and self.hits >= self.times:
            return False
        m = self.match
        if isinstance(m, str):
            return _norm(m) == _norm(text)
        if isinstance(m, re.Pattern):
            return m.search(text) is not None
        return bool(m(text))

    def describe(self) -> str:
        """Short human description, for error messages."""
        m = self.match
        if isinstance(m, str):
            return repr(_norm(m))
        if isinstance(m, re.Pattern):
            return f"re.compile({m.pattern!r})"
        return getattr(m, "__name__", repr(m))


@dataclass(slots=True)
class _Handler:
    method: str
    path: str | re.Pattern[str]
    response: Any
    times: int | None = None
    hits: int = 0

    def matches(self, method: str, path: str) -> bool:
        if self.times is not None and self.hits >= self.times:
            return False
        if self.method not in ("*", method):
            return False
        if isinstance(self.path, re.Pattern):
            return self.path.fullmatch(path) is not None
        return self.path == path


@dataclass(slots=True)
class _Failure:
    operation: str
    error: BaseException
    sobject: str | None
    remaining: int


@dataclass(slots=True)
class _Rejection:
    sobject: str | None
    error: SalesforceError
    when: Callable[[Mapping[str, Any]], bool] | None


@dataclass(frozen=True, slots=True)
class Who:
    """Caller and priority of a facade (``tagged()`` views carry their own)."""

    caller: str | None = None
    priority: Priority | None = None

    def resolve(self) -> tuple[str | None, str]:
        ctx = current_call_context()
        return self.caller or ctx.caller, self.priority or ctx.priority or "normal"


@dataclass(slots=True)
class _Locator:
    rows: list[dict[str, Any]]
    total: int
    page_size: int
    soql: str
    all_rows: bool


def _norm(soql: str) -> str:
    return " ".join(soql.split())


def _sobject_of(soql: str) -> str | None:
    m = _FROM.search(soql)
    return m.group(1) if m else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _detail(err: SalesforceError) -> ErrorDetail:
    return ErrorDetail(error_code=err.error_code, message=err.message, fields=err.fields)


def _as_error(error: BaseException | type[BaseException]) -> BaseException:
    if isinstance(error, type):
        return error(f"injected {error.__name__}")
    return error


def _get_field(rec: Mapping[str, Any], name: str) -> Any:
    if name in rec:
        return rec[name]
    low = name.lower()
    for k, v in rec.items():
        if k.lower() == low:
            return v
    return None


def _clean(data: Mapping[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(v) for k, v in data.items() if k not in ("attributes", "Id", "id")}


class FakeState:
    """Records, scripts, handlers, injected failures and recorded calls of one fake family."""

    def __init__(
        self,
        *,
        api_version: str,
        cursor_key: bytes | None,
        observers: Iterable[Observer],
        budget: Budget | None,
        key_prefixes: Mapping[str, str] | None,
        clock: Callable[[], datetime] | None,
    ) -> None:
        self.lock = threading.RLock()
        self.api_version = api_version
        self.cursor_key = cursor_key
        self.hub = ObserverHub(observers)
        self.usage = UsageTracker(budget)
        self.usage.bind(self._emit)
        self.clock: Callable[[], datetime] = clock or _utcnow
        self.key_prefixes = dict(key_prefixes or {})
        self.store: dict[str, dict[str, dict[str, Any]]] = {}
        self.modified: dict[str, dict[str, datetime]] = {}
        self.deleted: dict[str, dict[str, datetime]] = {}
        self.describes: dict[str, dict[str, Any]] = {}
        self.limits: dict[str, Limit] = {
            "DailyApiRequests": Limit(15000, 15000),
            "DataStorageMB": Limit(1024, 1024),
            "FileStorageMB": Limit(1024, 1024),
        }
        self.scripts: list[QueryScript] = []
        self.handlers: list[_Handler] = []
        self.failures: list[_Failure] = []
        self.rejections: list[_Rejection] = []
        self.calls: list[FakeCall] = []
        self.locators: dict[str, _Locator] = {}
        self._locator_seq = 0
        self.explain_plans: list[QueryPlan] = []
        self.counter = 0
        self.closed = False

    # --- plumbing -------------------------------------------------------------------

    def _emit(self, name: str, fields: Mapping[str, Any]) -> None:
        record_event(self.hub.emit(name, fields))

    def prefix(self, sobject: str) -> str:
        return self.key_prefixes.get(sobject) or key_prefix(sobject)

    def new_id(self, sobject: str) -> str:
        with self.lock:
            self.counter += 1
            return make_id(self.prefix(sobject), self.counter)

    def url(self, sobject: str, rid: str) -> str:
        return f"/services/data/v{self.api_version}/sobjects/{sobject}/{rid}"

    def run(
        self,
        who: Who,
        operation: str,
        sobject: str | None,
        args: Mapping[str, Any],
        fn: Callable[[], T],
        *,
        method: str,
        path: str,
        soql: str | None = None,
        records: int | None = None,
    ) -> T:
        """Record the call, apply injected failures and the budget guard, run ``fn``, emit events."""
        caller, priority = who.resolve()
        call = FakeCall(operation, sobject, dict(args), caller, priority)
        with self.lock:
            if self.closed:
                raise RuntimeError("the fake client is closed")
            self.calls.append(call)
        error: SalesforceError | None = None
        result: Any = None
        try:
            self.usage.check(priority, operation=operation)
            injected = self._pop_failure(operation, sobject)
            if injected is not None:
                raise injected
            with self.lock:
                result = fn()
            return result
        except SalesforceError as exc:
            if exc.operation is None:
                exc.operation = operation
            if exc.sobject is None:
                exc.sobject = sobject
            error = exc
            call.error = exc
            raise
        except BaseException as exc:
            call.error = exc
            raise
        finally:
            if call.error is None or error is not None:
                self._finish(operation, sobject, method, path, soql, records, result, error, caller, priority)

    def _finish(
        self,
        operation: str,
        sobject: str | None,
        method: str,
        path: str,
        soql: str | None,
        records: int | None,
        result: Any,
        error: SalesforceError | None,
        caller: str | None,
        priority: str,
    ) -> None:
        if records is None and error is None and isinstance(result, (QueryResult, BatchResult, list)):
            records = len(result)  # pyright: ignore[reportUnknownArgumentType]
        used, mx = self.usage.latest()
        self.usage.record_call(
            operation=operation,
            sobject=sobject,
            caller=caller,
            error_code=(error.error_code or type(error).__name__) if error else None,
            retries=0,
        )
        self._emit(
            "sf.request",
            {
                "operation": operation,
                "sobject": sobject,
                "method": method,
                "path_template": path_template(path),
                "status": (error.status if error else 200),
                "outcome": "error" if error else "ok",
                "error_code": error.error_code if error else None,
                "error_class": type(error).__name__ if error else None,
                "error_message": error.message[:200] if error and error.message else None,
                "duration_ms": 0.0,
                "attempts": 1,
                "retry_reasons": [],
                "refreshed": False,
                "records": records,
                "bytes_in": 0,
                "bytes_out": 0,
                "api_used": used,
                "api_max": mx,
                "caller": caller,
                "priority": priority,
                "soql_hash": soql_hash(soql) if soql else None,
                "request_id": None,
                "api_version": self.api_version,
                "client_id_hash": None,
                "fake": True,
            },
        )

    def _pop_failure(self, operation: str, sobject: str | None) -> BaseException | None:
        with self.lock:
            for f in self.failures:
                if f.operation not in ("*", operation):
                    continue
                if f.sobject is not None and f.sobject != sobject:
                    continue
                f.remaining -= 1
                if f.remaining <= 0:
                    self.failures.remove(f)
                    return f.error
                # A fresh copy per raise: one shared instance would chain tracebacks and
                # mutations (operation/sobject) across calls.
                return copy.copy(f.error)
        return None

    # --- controls -------------------------------------------------------------------

    def add_failure(
        self, operation: str, error: BaseException | type[BaseException], sobject: str | None, times: int
    ) -> None:
        if times < 1:
            raise ValueError("times must be >= 1")
        with self.lock:
            self.failures.append(_Failure(operation, _as_error(error), sobject, times))

    def add_rejection(
        self,
        sobject: str | None,
        error: SalesforceError,
        when: Callable[[Mapping[str, Any]], bool] | None,
    ) -> None:
        with self.lock:
            self.rejections.append(_Rejection(sobject, error, when))

    def seed(self, sobject: str, data: Mapping[str, Any]) -> str:
        check_sobject(sobject)
        with self.lock:
            rid = str(data.get("Id") or "") or self.new_id(sobject)
            check_record_id(rid)
            return self._put(sobject, rid, _clean(data))

    def _put(self, sobject: str, rid: str, data: dict[str, Any]) -> str:
        rec: dict[str, Any] = {"attributes": {"type": sobject, "url": self.url(sobject, rid)}, "Id": rid}
        rec.update(data)
        self.store.setdefault(sobject, {})[rid[:15]] = rec
        self.modified.setdefault(sobject, {})[rid[:15]] = self.clock()
        self.deleted.get(sobject, {}).pop(rid[:15], None)
        return rid

    def snapshot_records(self, sobject: str) -> list[dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(list(self.store.get(sobject, {}).values()))

    def snapshot_record(self, sobject: str, rid: str) -> dict[str, Any] | None:
        with self.lock:
            rec = self.store.get(sobject, {}).get(rid[:15])
            return copy.deepcopy(rec) if rec is not None else None

    # --- records --------------------------------------------------------------------

    def _check_rejections(self, sobject: str, data: Mapping[str, Any]) -> None:
        for r in self.rejections:
            if r.sobject is not None and r.sobject != sobject:
                continue
            if r.when is None or r.when(data):
                raise copy.copy(r.error)

    def find(self, sobject: str, rid: str, operation: str) -> dict[str, Any]:
        check_record_id(rid)
        rec = self.store.get(sobject, {}).get(rid[:15])
        if rec is not None:
            return rec
        if rid[:15] in self.deleted.get(sobject, {}):
            raise NotFound(
                "entity is deleted",
                error_code="ENTITY_IS_DELETED",
                status=404,
                operation=operation,
                sobject=sobject,
            )
        raise NotFound(
            "The requested resource does not exist",
            error_code="NOT_FOUND",
            status=404,
            operation=operation,
            sobject=sobject,
        )

    def project(self, rec: Mapping[str, Any], fields: Sequence[str] | None) -> dict[str, Any]:
        if not fields:
            return copy.deepcopy(dict(rec))
        out: dict[str, Any] = {"attributes": copy.deepcopy(rec["attributes"])}
        for f in fields:
            soql_identifier(f)
            out[f] = copy.deepcopy(_get_field(rec, f))
        return out

    def get(
        self,
        sobject: str,
        rid: str,
        fields: Sequence[str] | None,
        model: Any,
        if_modified_since: datetime | None,
    ) -> Any:
        rec = self.find(sobject, rid, "sobject.get")
        if if_modified_since is not None:
            changed = self.modified.get(sobject, {}).get(rid[:15])
            if changed is not None and changed <= if_modified_since:
                return None
        return apply_model([self.project(rec, fields)], model)[0]

    def matching(self, sobject: str, field: str, value: Any) -> list[dict[str, Any]]:
        soql_identifier(field)
        return [r for r in self.store.get(sobject, {}).values() if _get_field(r, field) == value]

    def get_by_external_id(
        self, sobject: str, field: str, value: str, fields: Sequence[str] | None, model: Any
    ) -> Any:
        found = self.matching(sobject, field, value)
        if not found:
            raise NotFound(
                f"Provided external ID field does not exist or is not accessible: {value}",
                error_code="NOT_FOUND",
                status=404,
                operation="sobject.get_by_external_id",
                sobject=sobject,
            )
        if len(found) > 1:
            raise self._multiple(sobject, found, "sobject.get_by_external_id")
        return apply_model([self.project(found[0], fields)], model)[0]

    def _multiple(self, sobject: str, found: list[dict[str, Any]], operation: str) -> MultipleMatches:
        ids = tuple(str(r["Id"]) for r in found)
        return MultipleMatches(
            f"{len(ids)} records match the external id",
            error_code="MULTIPLE_CHOICES",
            status=300,
            operation=operation,
            sobject=sobject,
            matches=ids,
        )

    def create(self, sobject: str, data: Mapping[str, Any], operation: str = "sobject.create") -> str:
        if not isinstance(data, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("data must be a mapping of field values")
        if any(k.lower() == "id" for k in data):
            raise InvalidField(
                "Unable to create/update fields: Id",
                error_code="INVALID_FIELD_FOR_INSERT_UPDATE",
                fields=("Id",),
                status=400,
                operation=operation,
                sobject=sobject,
            )
        self._check_rejections(sobject, data)
        return self._put(sobject, self.new_id(sobject), _clean(data))

    def update(
        self,
        sobject: str,
        rid: str,
        data: Mapping[str, Any],
        if_unmodified_since: datetime | None = None,
        operation: str = "sobject.update",
    ) -> str:
        rec = self.find(sobject, rid, operation)
        if if_unmodified_since is not None:
            changed = self.modified.get(sobject, {}).get(rid[:15])
            if changed is not None and changed > if_unmodified_since:
                raise Conflict(
                    "The record was modified after the If-Unmodified-Since date",
                    error_code="PRECONDITION_FAILED",
                    status=412,
                    operation=operation,
                    sobject=sobject,
                )
        self._check_rejections(sobject, {**data, "Id": rec["Id"]})
        rec.update(_clean(data))
        self.modified.setdefault(sobject, {})[rid[:15]] = self.clock()
        return str(rec["Id"])

    def upsert(
        self, sobject: str, field: str, value: Any, data: Mapping[str, Any], operation: str = "sobject.upsert"
    ) -> UpsertResult:
        found = self.matching(sobject, field, value)
        if len(found) > 1:
            raise self._multiple(sobject, found, operation)
        if found:
            rid = self.update(sobject, str(found[0]["Id"]), data, operation=operation)
            return UpsertResult(id=rid, created=False)
        payload = {k: v for k, v in data.items() if k.lower() != field.lower()}
        payload[field] = value
        return UpsertResult(id=self.create(sobject, payload, operation), created=True)

    def delete(
        self,
        sobject: str,
        rid: str,
        operation: str = "sobject.delete",
    ) -> None:
        self.find(sobject, rid, operation)
        del self.store[sobject][rid[:15]]
        self.deleted.setdefault(sobject, {})[rid[:15]] = self.clock()

    def sobject_of_id(self, rid: str) -> str | None:
        key = rid[:15]
        for name, recs in self.store.items():
            if key in recs:
                return name
        for name, gone in self.deleted.items():
            if key in gone:
                return name
        return None

    def describe(self, sobject: str) -> dict[str, Any]:
        if sobject in self.describes:
            return copy.deepcopy(self.describes[sobject])
        names: dict[str, None] = {"Id": None}
        for rec in self.store.get(sobject, {}).values():
            for k in rec:
                if k != "attributes":
                    names[k] = None
        return {
            "name": sobject,
            "label": sobject,
            "keyPrefix": self.prefix(sobject),
            "custom": sobject.endswith("__c"),
            "createable": True,
            "updateable": True,
            "deletable": True,
            "queryable": True,
            "fields": [{"name": n, "type": "id" if n == "Id" else "string"} for n in names],
            "urls": {"sobject": f"/services/data/v{self.api_version}/sobjects/{sobject}"},
        }

    def describe_global(self) -> dict[str, Any]:
        names = sorted(set(self.store) | set(self.describes) | set(self.deleted))
        return {
            "encoding": "UTF-8",
            "maxBatchSize": 200,
            "sobjects": [
                {"name": n, "label": n, "keyPrefix": self.prefix(n), "custom": n.endswith("__c")}
                for n in names
            ],
        }

    def metadata(self, sobject: str) -> dict[str, Any]:
        d = self.describe(sobject)
        return {
            "objectDescribe": {k: v for k, v in d.items() if k != "fields"},
            "recentItems": [],
        }

    def updated(self, sobject: str, start: datetime, end: datetime) -> dict[str, Any]:
        ids = [
            str(self.store[sobject][k]["Id"])
            for k, t in self.modified.get(sobject, {}).items()
            if start <= t <= end and k in self.store.get(sobject, {})
        ]
        return {"ids": ids, "latestDateCovered": format_sf_datetime(end)}

    def deleted_between(self, sobject: str, start: datetime, end: datetime) -> dict[str, Any]:
        out = [
            {"id": k, "deletedDate": format_sf_datetime(t)}
            for k, t in self.deleted.get(sobject, {}).items()
            if start <= t <= end
        ]
        return {
            "deletedRecords": out,
            "earliestDateAvailable": format_sf_datetime(start),
            "latestDateCovered": format_sf_datetime(end),
        }

    # --- query & search -------------------------------------------------------------

    def add_script(self, script: QueryScript) -> QueryScript:
        with self.lock:
            self.scripts.insert(0, script)  # newest first
        return script

    def _script_for(self, kind: str, text: str) -> QueryScript:
        for s in self.scripts:
            if s.kind == kind and s.matches(text):
                s.hits += 1
                return s
        known = [s.describe() for s in self.scripts if s.kind == kind]
        hint = "fake.on_query(...)" if kind == "query" else "fake.on_search(...)"
        raise UnscriptedCall(
            f"no {kind} script matches {_norm(text)!r}. Register one with {hint}"
            + (f"; registered: {', '.join(known)}" if known else "; none registered")
        )

    def _rows(self, script: QueryScript, text: str, sobject: str | None) -> list[dict[str, Any]]:
        raw = script.rows(text) if callable(script.rows) else script.rows
        out: list[dict[str, Any]] = []
        for row in raw:
            r = copy.deepcopy(dict(row))
            if "attributes" not in r and sobject:
                attrs: dict[str, Any] = {"type": sobject}
                if r.get("Id"):
                    attrs["url"] = self.url(sobject, str(r["Id"]))
                r = {"attributes": attrs, **r}
            out.append(r)
        return out

    @staticmethod
    def page_size(page_size: int | None) -> int:
        if page_size is None:
            return DEFAULT_PAGE_SIZE
        if not 200 <= page_size <= 2000:
            raise ValueError("page_size must be between 200 and 2000")
        return page_size

    def query_rows(self, soql: str) -> tuple[list[dict[str, Any]], int]:
        script = self._script_for("query", soql)
        rows = self._rows(script, soql, _sobject_of(soql))
        return rows, script.total_size if script.total_size is not None else len(rows)

    def first_page(
        self, soql: str, page_size: int | None, include_deleted: bool
    ) -> tuple[list[dict[str, Any]], int, bool, str | None]:
        ps = self.page_size(page_size)
        rows, total = self.query_rows(soql)
        loc = _Locator(rows, total, ps, soql, include_deleted)
        return self._slice(loc, None, 0, ps)

    def _slice(
        self, loc: _Locator, loc_id: str | None, offset: int, ps: int
    ) -> tuple[list[dict[str, Any]], int, bool, str | None]:
        chunk = copy.deepcopy(loc.rows[offset : offset + ps])
        done = offset + ps >= len(loc.rows)
        nxt: str | None = None
        if not done:
            if loc_id is None:
                self._locator_seq += 1
                loc_id = make_id("01g", self._locator_seq)[:15]
                self.locators[loc_id] = loc
                while len(self.locators) > MAX_OPEN_LOCATORS:  # bounded: oldest are dropped first
                    del self.locators[next(iter(self.locators))]
            kind = "queryAll" if loc.all_rows else "query"
            nxt = f"/services/data/v{self.api_version}/{kind}/{loc_id}-{offset + ps}"
        return chunk, loc.total, done, nxt

    def next_page(
        self, path: str, page_size: int | None
    ) -> tuple[list[dict[str, Any]], int, bool, str | None, str]:
        m = _LOCATOR.search(path)
        loc = self.locators.get(m.group(1)) if m else None
        if m is None or loc is None:
            raise MalformedQuery(
                "invalid query locator",
                error_code="INVALID_QUERY_LOCATOR",
                status=400,
                operation="query_more",
            )
        ps = self.page_size(page_size) if page_size is not None else loc.page_size
        rows, total, done, nxt = self._slice(loc, m.group(1), int(m.group(2)), ps)
        return rows, total, done, nxt, loc.soql

    def to_result(
        self, rows: list[dict[str, Any]], total: int, done: bool, nxt: str | None, model: Any
    ) -> QueryResult[Any]:
        cursor: Cursor | None = encode_cursor(nxt, self.cursor_key) if nxt else None
        return QueryResult(records=apply_model(rows, model), total_size=total, done=done, cursor=cursor)

    def decode(self, cursor: Cursor | str) -> str:
        return decode_cursor(cursor, self.cursor_key)

    def search(self, text: str) -> list[dict[str, Any]]:
        script = self._script_for("search", text)
        return self._rows(script, text, None)

    # --- collections ----------------------------------------------------------------

    def _restore(self, snap: tuple[Any, Any, Any]) -> None:
        self.store, self.modified, self.deleted = snap

    def _snap(self) -> tuple[Any, Any, Any]:
        return copy.deepcopy((self.store, self.modified, self.deleted))

    def batch(
        self,
        items: Sequence[T],
        one: Callable[[T], RecordResult],
        all_or_none: bool,
        operation: str,
        sobject: str | None,
        id_of: Callable[[T], str | None] = lambda _item: None,
    ) -> BatchResult[RecordResult]:
        results: list[RecordResult] = []
        for chunk in chunked(items, COLLECTION_WRITE_CHUNK):
            snap = self._snap() if all_or_none else None
            part: list[RecordResult] = []
            first_error: SalesforceError | None = None
            for item in chunk:
                try:
                    part.append(one(item))
                except SalesforceError as exc:
                    first_error = first_error or exc
                    part.append(RecordResult(ok=False, id=id_of(item), errors=(_detail(exc),)))
            if all_or_none and first_error is not None and snap is not None:
                self._restore(snap)
                first_error.operation = operation
                first_error.sobject = first_error.sobject or sobject
                raise first_error
            results.extend(part)
        return BatchResult(results, operation=operation, sobject=sobject)

    def coll_create(
        self, rows: Iterable[Mapping[str, Any]], sobject: str | None, all_or_none: bool
    ) -> BatchResult[RecordResult]:
        typed = [with_type(r, check_sobject(sobject) if sobject else None) for r in rows]

        def one(row: dict[str, Any]) -> RecordResult:
            name = check_sobject(str(row["attributes"]["type"]))
            data = {k: v for k, v in row.items() if k != "attributes"}
            return RecordResult(ok=True, id=self.create(name, data, "collections.create"))

        return self.batch(typed, one, all_or_none, "collections.create", sobject)

    def coll_update(
        self, sobject: str, rows: Iterable[Mapping[str, Any]], all_or_none: bool
    ) -> BatchResult[RecordResult]:
        check_sobject(sobject)
        typed: list[dict[str, Any]] = []
        for r in rows:
            if not r.get("Id"):
                raise ValueError("each row needs an Id for update")
            typed.append(with_type(r, sobject))

        def one(row: dict[str, Any]) -> RecordResult:
            name = str(row["attributes"]["type"])
            rid = str(row["Id"])
            return RecordResult(ok=True, id=self.update(name, rid, row, operation="collections.update"))

        return self.batch(
            typed, one, all_or_none, "collections.update", sobject, id_of=lambda r: str(r["Id"])
        )

    def coll_upsert(
        self, sobject: str, field: str, rows: Iterable[Mapping[str, Any]], all_or_none: bool
    ) -> BatchResult[RecordResult]:
        check_sobject(sobject)
        soql_identifier(field)
        typed = [with_type(r, sobject) for r in rows]

        def one(row: dict[str, Any]) -> RecordResult:
            value = _get_field(row, field)
            if value is None:
                raise RequiredFieldMissing(
                    f"Required fields are missing: [{field}]",
                    error_code="REQUIRED_FIELD_MISSING",
                    fields=(field,),
                    operation="collections.upsert",
                    sobject=sobject,
                )
            data = {k: v for k, v in row.items() if k != "attributes"}
            res = self.upsert(sobject, field, value, data, "collections.upsert")
            return RecordResult(ok=True, id=res.id, created=res.created)

        return self.batch(typed, one, all_or_none, "collections.upsert", sobject)

    def coll_delete(self, ids: Iterable[str], all_or_none: bool) -> BatchResult[RecordResult]:
        id_list = [check_record_id(i) for i in ids]

        def one(rid: str) -> RecordResult:
            name = self.sobject_of_id(rid)
            if name is None:
                raise NotFound(
                    "invalid cross reference id",
                    error_code="INVALID_CROSS_REFERENCE_KEY",
                    operation="collections.delete",
                )
            rec = self.store.get(name, {}).get(rid[:15])
            full = str(rec["Id"]) if rec else rid
            self.delete(name, rid, operation="collections.delete")
            return RecordResult(ok=True, id=full)

        return self.batch(id_list, one, all_or_none, "collections.delete", None, id_of=lambda r: r)

    def coll_retrieve(
        self, sobject: str, ids: Iterable[str], fields: Sequence[str]
    ) -> list[dict[str, Any] | None]:
        check_sobject(sobject)
        field_list = [soql_identifier(f) for f in fields]
        if not field_list:
            raise ValueError("fields must not be empty")
        out: list[dict[str, Any] | None] = []
        for rid in ids:
            check_record_id(rid)
            rec = self.store.get(sobject, {}).get(rid[:15])
            out.append(self.project(rec, field_list) if rec is not None else None)
        return out

    # --- raw & limits ---------------------------------------------------------------

    def normalise_path(self, path: str) -> str:
        build_url("https://fake.invalid", self.api_version, path)  # same validation as the client
        p = path.split("?", 1)[0]
        if _VERSIONED.match(p):
            return _VERSIONED.sub("", p)
        return p

    def add_handler(self, method: str, path: str | re.Pattern[str], response: Any, times: int | None) -> None:
        norm = self.normalise_path(path) if isinstance(path, str) else path
        with self.lock:
            self.handlers.insert(0, _Handler(method.upper(), norm, response, times))

    def handle(self, req: FakeRequest) -> Any:
        for h in self.handlers:
            if h.matches(req.method, req.path):
                h.hits += 1
                resp = h.response
                if isinstance(resp, BaseException):
                    raise resp
                if callable(resp):
                    resp = resp(req)
                return copy.deepcopy(resp)
        raise UnscriptedCall(
            f"no handler for {req.method} {req.path}. Register one with fake.on_request(method, path, ...)"
        )

    def get_limits(self) -> dict[str, Limit]:
        out = dict(self.limits)
        daily = out.get("DailyApiRequests")
        if daily is not None:
            self.usage.observe(LimitInfo(daily.used, daily.max))
        return out
