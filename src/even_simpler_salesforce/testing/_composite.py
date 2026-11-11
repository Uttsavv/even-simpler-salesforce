"""Fake of ``sf.composite`` (§14): Composite, Composite Graph, Batch and sObject Tree.

Subrequests run against the fake's in-memory records with the same builders, limits and result
types as the real client. References (``@{ref.field}``) are resolved from earlier results.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from typing import Any, cast

from ..core.composite import (
    BatchRequest,
    CompositeRequest,
    GraphRequest,
    Subrequest,
    batch_payload,
    composite_payload,
    graph_payload,
    subrequest_error,
    tree_payload,
)
from ..errors import ErrorDetail, NotFound, SalesforceError
from ..models import CompositeResult, GraphResult, SubrequestResult
from ._state import FakeRequest, FakeState, Who

__all__ = [
    "AsyncFakeBatchBuilder",
    "AsyncFakeComposite",
    "AsyncFakeCompositeBuilder",
    "AsyncFakeGraphBuilder",
    "FakeBatchBuilder",
    "FakeComposite",
    "FakeCompositeBuilder",
    "FakeGraphBuilder",
]

_REFERENCE = re.compile(r"@\{(?P<ref>[A-Za-z][A-Za-z0-9_]*)\.(?P<path>[^{}]+)\}")
_STEP = re.compile(r"([A-Za-z0-9_]+)|\[(\d+)\]")
_ROLLED_BACK = "The transaction was rolled back since another operation in the same transaction failed."


class _Halted(Exception):
    """A subrequest that can't run because a reference it uses has no value."""


def _lookup(body: Any, path: str) -> Any:
    value = body
    for name, index in _STEP.findall(path):
        if name:
            if not isinstance(value, Mapping):
                raise KeyError(path)
            m = cast("Mapping[str, Any]", value)
            if name not in m:
                raise KeyError(path)
            value = m[name]
        else:
            if not isinstance(value, list) or int(index) >= len(cast("list[Any]", value)):
                raise KeyError(path)
            value = cast("list[Any]", value)[int(index)]
    return value


def _resolve(value: Any, done: Mapping[str, SubrequestResult]) -> Any:
    """Replace ``@{ref.field}`` with values from earlier results (a full match keeps the type)."""
    if isinstance(value, str):

        def one(m: re.Match[str]) -> Any:
            res = done.get(m.group("ref"))
            if res is None or not res.ok:
                raise _Halted(f"Invalid reference specified. No value for {m.group(0)} found.")
            try:
                return _lookup(res.body, m.group("path"))
            except KeyError:
                raise _Halted(f"Invalid reference specified. No value for {m.group(0)} found.") from None

        full = _REFERENCE.fullmatch(value)
        if full is not None:
            return one(full)
        return _REFERENCE.sub(lambda m: str(one(m)), value)
    if isinstance(value, Mapping):
        return {k: _resolve(v, done) for k, v in cast("Mapping[str, Any]", value).items()}
    if isinstance(value, list):
        return [_resolve(v, done) for v in cast("list[Any]", value)]
    return value


def _entries(details: Iterable[ErrorDetail]) -> list[dict[str, Any]]:
    return [{"errorCode": d.error_code, "message": d.message, "fields": list(d.fields)} for d in details]


def _failed(sub: Subrequest, exc: SalesforceError, operation: str) -> SubrequestResult:
    err = copy.copy(exc)
    base = exc.errors or (ErrorDetail(exc.error_code, exc.message, exc.fields),)
    err.errors = tuple(
        ErrorDetail(d.error_code, d.message, d.fields, {**d.extended, "referenceId": sub.reference_id})
        for d in base
    )
    err.operation = operation
    status = (
        exc.status
        if exc.status is not None and exc.status >= 300
        else (404 if isinstance(exc, NotFound) else 400)
    )
    err.status = status
    return SubrequestResult(sub.reference_id, status, _entries(base), {}, err)


def _halted(
    sub: Subrequest, operation: str, message: str, code: str = "PROCESSING_HALTED", status: int = 400
) -> SubrequestResult:
    body: list[dict[str, Any]] = [{"errorCode": code, "message": message, "fields": []}]
    err = subrequest_error(sub, status, body, operation=operation, reference_id=sub.reference_id)
    return SubrequestResult(sub.reference_id, status, body, {}, err)


def _execute(
    state: FakeState, sub: Subrequest, done: Mapping[str, SubrequestResult], operation: str
) -> SubrequestResult:
    """Run one subrequest; a ``SalesforceError`` or :class:`_Halted` becomes a failed result."""
    try:
        args = cast("dict[str, Any]", _resolve(dict(sub.args), done))
        body = _resolve(sub.body, done)
        ref = sub.reference_id
        name = sub.sobject or ""
        if sub.action == "create":
            rid = state.create(name, body, operation)
            return SubrequestResult(
                ref, 201, {"id": rid, "success": True, "errors": []}, {"Location": state.url(name, rid)}
            )
        if sub.action == "update":
            state.update(name, str(args["id"]), body, operation=operation)
            return SubrequestResult(ref, 204)
        if sub.action == "upsert":
            res = state.upsert(name, str(args["field"]), args["value"], body, operation)
            out: dict[str, Any] = {"id": res.id, "success": True, "errors": [], "created": res.created}
            return SubrequestResult(ref, 201 if res.created else 200, out)
        if sub.action == "delete":
            state.delete(name, str(args["id"]), operation=operation)
            return SubrequestResult(ref, 204)
        if sub.action == "get":
            rec = state.get(name, str(args["id"]), args.get("fields") or None, None, None)
            return SubrequestResult(ref, 200, rec)
        if sub.action == "query":
            rows, total = state.query_rows(str(args["soql"]))
            return SubrequestResult(ref, 200, {"totalSize": total, "done": True, "records": rows})
        path, _, query = cast(str, _resolve(sub.path, done)).partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        result = state.handle(FakeRequest(sub.method, path, params, body, None, {}))
        return SubrequestResult(ref, 204 if result is None else 200, result)
    except _Halted as halted:
        return _halted(sub, operation, str(halted))
    except SalesforceError as exc:
        return _failed(sub, exc, operation)


def _run_atomic(
    state: FakeState, subs: list[Subrequest], operation: str, all_or_none: bool
) -> list[SubrequestResult]:
    """Run subrequests in order. With ``all_or_none``, a failure rolls back and halts the rest."""
    snap = state._snap() if all_or_none else None  # pyright: ignore[reportPrivateUsage]
    done: dict[str, SubrequestResult] = {}
    out: list[SubrequestResult] = []
    failed = False
    for sub in subs:
        if failed and all_or_none:
            out.append(_halted(sub, operation, _ROLLED_BACK))
            continue
        res = _execute(state, sub, done, operation)
        failed = failed or not res.ok
        done[sub.reference_id] = res
        out.append(res)
    if failed and snap is not None:
        state._restore(snap)  # pyright: ignore[reportPrivateUsage]
        out = [r if not r.ok else _halted(s, operation, _ROLLED_BACK) for s, r in zip(subs, out, strict=True)]
    return out


def _args(subs: Iterable[Subrequest]) -> list[dict[str, Any]]:
    return [
        {"method": s.method, "path": s.path, "reference_id": s.reference_id, "body": s.body} for s in subs
    ]


def _send_composite(state: FakeState, who: Who, req: CompositeRequest) -> CompositeResult:
    composite_payload(req, state.api_version)  # same checks as the real client
    op = "composite.send"
    subs = req.subrequests
    return state.run(
        who,
        op,
        None,
        {"subrequests": _args(subs), "all_or_none": req.all_or_none},
        lambda: CompositeResult(_run_atomic(state, subs, op, req.all_or_none), operation=op),
        method="POST",
        path="composite",
        records=len(subs),
    )


def _send_graphs(state: FakeState, who: Who, req: GraphRequest) -> list[GraphResult]:
    graph_payload(req, state.api_version)
    op = "composite.graph"

    def run() -> list[GraphResult]:
        out: list[GraphResult] = []
        for g in req.graphs:
            results = CompositeResult(_run_atomic(state, g.subrequests, op, True), operation=op)
            out.append(GraphResult(g.graph_id, not results.failed, results))
        return out

    return state.run(
        who,
        op,
        None,
        {"graphs": {g.graph_id: _args(g.subrequests) for g in req.graphs}},
        run,
        method="POST",
        path="composite/graph",
        records=sum(len(g) for g in req.graphs),
    )


def _send_batch(state: FakeState, who: Who, req: BatchRequest) -> CompositeResult:
    batch_payload(req, state.api_version)
    op = "composite.batch"
    subs = req.subrequests

    def run() -> CompositeResult:
        out: list[SubrequestResult] = []
        halted = False
        for sub in subs:
            if halted:
                out.append(
                    _halted(
                        sub,
                        op,
                        "Batch processing halted: an earlier subrequest failed and haltOnError is true.",
                        "BATCH_PROCESSING_HALTED",
                        412,
                    )
                )
                continue
            res = _execute(state, sub, {}, op)
            halted = req.halt_on_error and not res.ok
            out.append(res)
        return CompositeResult(out, operation=op)

    return state.run(
        who,
        op,
        None,
        {"subrequests": _args(subs), "halt_on_error": req.halt_on_error},
        run,
        method="POST",
        path="composite/batch",
        records=len(subs),
    )


def _parent_field(parent_type: str) -> str:
    return parent_type if parent_type.endswith("__c") else f"{parent_type}Id"


def _tree(state: FakeState, who: Who, sobject: str, records: list[dict[str, Any]]) -> dict[str, str]:
    payload, _ = tree_payload(sobject, records)
    op = "composite.tree"
    created: dict[str, str] = {}

    def create(row: Mapping[str, Any], parent: tuple[str, str] | None) -> None:
        attrs = cast("Mapping[str, Any]", row["attributes"])
        rtype, ref = str(attrs["type"]), str(attrs["referenceId"])
        data: dict[str, Any] = {}
        children: list[tuple[str, list[Any]]] = []
        for key, value in row.items():
            if key == "attributes":
                continue
            if isinstance(value, Mapping) and "records" in value:
                children.append((key, cast("list[Any]", cast("Mapping[str, Any]", value)["records"])))
            else:
                data[key] = value
        if parent is not None:
            data.setdefault(_parent_field(parent[0]), parent[1])
        try:
            rid = state.create(rtype, data, op)
        except SalesforceError as exc:
            err = copy.copy(exc)
            base = exc.errors or (ErrorDetail(exc.error_code, exc.message, exc.fields),)
            err.errors = tuple(
                ErrorDetail(d.error_code, d.message, d.fields, {**d.extended, "referenceId": ref})
                for d in base
            )
            err.status = 400
            raise err from None
        created[ref] = rid
        for _, kids in children:
            for kid in kids:
                create(cast("Mapping[str, Any]", kid), (rtype, rid))

    def run() -> dict[str, str]:
        snap = state._snap()  # pyright: ignore[reportPrivateUsage]
        try:
            for root in cast("list[Mapping[str, Any]]", payload["records"]):
                create(root, None)
        except SalesforceError:
            state._restore(snap)  # pyright: ignore[reportPrivateUsage]
            raise
        return created

    return state.run(
        who,
        op,
        sobject,
        {"records": records},
        run,
        method="POST",
        path=f"composite/tree/{sobject}",
        records=len(created) or None,
    )


# --- facades ---------------------------------------------------------------------------------


class FakeCompositeBuilder(CompositeRequest):
    """Fake of the builder from ``sf.composite.builder()``; :meth:`send` runs against the fake."""

    def __init__(self, state: FakeState, who: Who, *, all_or_none: bool, collate_subrequests: bool) -> None:
        super().__init__(all_or_none=all_or_none, collate_subrequests=collate_subrequests)
        self._state = state
        self._who = who

    def send(self) -> CompositeResult:
        """Run the subrequests in order. ``all_or_none`` rolls back the fake's records too."""
        return _send_composite(self._state, self._who, self)


class FakeGraphBuilder(GraphRequest):
    """Fake of the builder from ``sf.composite.graph()``. Each graph is rolled back on its own."""

    def __init__(self, state: FakeState, who: Who) -> None:
        super().__init__()
        self._state = state
        self._who = who

    def send(self) -> list[GraphResult]:
        """Run every graph; a failed graph leaves no records behind."""
        return _send_graphs(self._state, self._who, self)


class FakeBatchBuilder(BatchRequest):
    """Fake of the builder from ``sf.composite.batch()``."""

    def __init__(self, state: FakeState, who: Who, *, halt_on_error: bool) -> None:
        super().__init__(halt_on_error=halt_on_error)
        self._state = state
        self._who = who

    def send(self) -> CompositeResult:
        """Run each subrequest on its own; ``halt_on_error`` skips the rest after a failure."""
        return _send_batch(self._state, self._who, self)


class FakeComposite:
    """Fake of ``sf.composite``. Same builders, limits and results as the real client.

    Subrequests act on the fake's records: ``fake.reject(...)`` and ``fake.on_query(...)``
    apply inside them, and ``fake.fail_next("composite.send", error)`` fails the whole call.
    In :meth:`tree`, a child record gets its parent's id in ``<ParentType>Id`` (or
    ``<Parent__c>`` for a custom parent) unless it sets the field itself.
    """

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    def builder(
        self, *, all_or_none: bool = False, collate_subrequests: bool = False
    ) -> FakeCompositeBuilder:
        """See ``sf.composite.builder()``."""
        return FakeCompositeBuilder(
            self._state, self._who, all_or_none=all_or_none, collate_subrequests=collate_subrequests
        )

    def graph(self) -> FakeGraphBuilder:
        """See ``sf.composite.graph()``."""
        return FakeGraphBuilder(self._state, self._who)

    def batch(self, *, halt_on_error: bool = False) -> FakeBatchBuilder:
        """See ``sf.composite.batch()``."""
        return FakeBatchBuilder(self._state, self._who, halt_on_error=halt_on_error)

    def tree(self, sobject: str, records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
        """Create the record trees in one transaction; ``{referenceId: id}``."""
        return _tree(self._state, self._who, sobject, [dict(r) for r in records])


class AsyncFakeCompositeBuilder(CompositeRequest):
    """Async fake of the builder from ``sf.composite.builder()``."""

    def __init__(self, state: FakeState, who: Who, *, all_or_none: bool, collate_subrequests: bool) -> None:
        super().__init__(all_or_none=all_or_none, collate_subrequests=collate_subrequests)
        self._state = state
        self._who = who

    async def send(self) -> CompositeResult:
        """See :meth:`FakeCompositeBuilder.send`."""
        return _send_composite(self._state, self._who, self)


class AsyncFakeGraphBuilder(GraphRequest):
    """Async fake of the builder from ``sf.composite.graph()``."""

    def __init__(self, state: FakeState, who: Who) -> None:
        super().__init__()
        self._state = state
        self._who = who

    async def send(self) -> list[GraphResult]:
        """See :meth:`FakeGraphBuilder.send`."""
        return _send_graphs(self._state, self._who, self)


class AsyncFakeBatchBuilder(BatchRequest):
    """Async fake of the builder from ``sf.composite.batch()``."""

    def __init__(self, state: FakeState, who: Who, *, halt_on_error: bool) -> None:
        super().__init__(halt_on_error=halt_on_error)
        self._state = state
        self._who = who

    async def send(self) -> CompositeResult:
        """See :meth:`FakeBatchBuilder.send`."""
        return _send_batch(self._state, self._who, self)


class AsyncFakeComposite:
    """Async fake of ``sf.composite``; see :class:`FakeComposite`."""

    def __init__(self, inner: FakeComposite) -> None:
        self._inner = inner

    def builder(
        self, *, all_or_none: bool = False, collate_subrequests: bool = False
    ) -> AsyncFakeCompositeBuilder:
        """See ``sf.composite.builder()``."""
        i = self._inner
        return AsyncFakeCompositeBuilder(
            i._state,
            i._who,
            all_or_none=all_or_none,
            collate_subrequests=collate_subrequests,  # pyright: ignore[reportPrivateUsage]
        )

    def graph(self) -> AsyncFakeGraphBuilder:
        """See ``sf.composite.graph()``."""
        return AsyncFakeGraphBuilder(self._inner._state, self._inner._who)  # pyright: ignore[reportPrivateUsage]

    def batch(self, *, halt_on_error: bool = False) -> AsyncFakeBatchBuilder:
        """See ``sf.composite.batch()``."""
        return AsyncFakeBatchBuilder(self._inner._state, self._inner._who, halt_on_error=halt_on_error)  # pyright: ignore[reportPrivateUsage]

    async def tree(self, sobject: str, records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
        """See :meth:`FakeComposite.tree`."""
        return self._inner.tree(sobject, records)
