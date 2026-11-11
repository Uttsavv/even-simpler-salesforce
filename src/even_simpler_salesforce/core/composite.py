"""Composite family (§9.4): subrequest builders, client-side limits and result parsing. No I/O.

The wire shapes follow the REST API Developer Guide. developer.salesforce.com answers automated
clients with 403, so the pages were read through search excerpts (2026-11-10); what is still
open is listed in big-bang.md §21 (V55-V59).

- Composite: https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_composite_post.htm
- Composite request body: https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/requests_composite.htm
- allOrNone: https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_allornone.htm
- Composite Graph: https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_composite_graph.htm
- sObject Tree: https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/requests_composite_sobject_tree.htm
- Batch: https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/requests_composite_batch.htm
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast
from urllib.parse import quote

from ..errors import ErrorDetail, SalesforceError, UnexpectedResponse
from ..models import CompositeResult, GraphResult, SubrequestResult
from .classify import error_from_detail, parse_details
from .http import RequestSpec, check_record_id, check_sobject, url_segment
from .soql import soql_identifier, soql_values

__all__ = [
    "BATCH_MAX",
    "COMPOSITE_MAX",
    "COMPOSITE_MAX_HEAVY",
    "GRAPH_MAX",
    "TREE_MAX_DEPTH",
    "TREE_MAX_RECORDS",
    "TREE_MAX_TYPES",
    "BatchRequest",
    "CompositeRequest",
    "Graph",
    "GraphRequest",
    "Reference",
    "Subrequest",
    "Subrequests",
    "batch_payload",
    "composite_payload",
    "graph_payload",
    "is_read_only",
    "parse_batch",
    "parse_composite",
    "parse_graphs",
    "parse_tree",
    "references_in",
    "subrequest_error",
    "tree_payload",
]

COMPOSITE_MAX = 25
"""Subrequests per Composite request."""
COMPOSITE_MAX_HEAVY = 5
"""Composite subrequests that may be sObject Collections or query operations."""
GRAPH_MAX = 500
"""Subrequests per graph of a Composite Graph request."""
BATCH_MAX = 25
"""Subrequests per Batch request."""
TREE_MAX_RECORDS = 200
"""Records per sObject Tree request, across all trees."""
TREE_MAX_TYPES = 5
"""Different sObject types per sObject Tree request."""
TREE_MAX_DEPTH = 5
"""Levels per tree (the root record is level 1)."""

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_REF_FIELD = re.compile(r"^[A-Za-z0-9_]+(?:\[\d+\])*(?:\.[A-Za-z0-9_]+(?:\[\d+\])*)*$")
_REFERENCE = re.compile(r"@\{(?P<ref>[A-Za-z][A-Za-z0-9_]*)\.(?P<path>[^{}]+)\}")
_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})
_SPEC_OPERATION = {"composite": "composite.send", "graph": "composite.graph", "batch": "composite.batch"}


@dataclass(frozen=True, slots=True)
class Reference:
    """A handle on one subrequest. Pass ``ref(field)`` to a later subrequest to use its result.

    Example:
        acc = c.create("Account", {"Name": "Acme"})
        c.create("Contact", {"LastName": "Doe", "AccountId": acc.ref("id")})

    Attributes:
        reference_id: The subrequest's ``referenceId``.
    """

    reference_id: str

    def ref(self, field: str) -> str:
        """``@{referenceId.field}``: Salesforce replaces it with that value of this result.

        Args:
            field: A field of the result body, such as ``"id"`` for a create or
                ``"records[0].Id"`` for a query.
        """
        if not isinstance(field, str) or not _REF_FIELD.fullmatch(field):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError(f"invalid reference field: {field!r}")
        return f"@{{{self.reference_id}.{field}}}"


@dataclass(frozen=True, slots=True)
class Subrequest:
    """One subrequest. ``path`` is relative to ``/services/data/vXX.X/``.

    ``action``, ``sobject`` and ``args`` describe the call for the test fake and for errors.
    """

    method: str
    path: str
    reference_id: str
    body: Any = None
    kind: str = "sobject"  # "sobject", "collections", "query" or "other"
    action: str = "request"
    sobject: str | None = None
    args: Mapping[str, Any] = field(default_factory=lambda: {})


def references_in(value: Any) -> set[str]:
    """Reference ids used (``@{id.field}``) anywhere in ``value``: strings, dicts and lists."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(m.group("ref") for m in _REFERENCE.finditer(value))
    elif isinstance(value, Mapping):
        for k, v in cast("Mapping[Any, Any]", value).items():
            found |= references_in(k)
            found |= references_in(v)
    elif isinstance(value, (list, tuple)):
        for v in cast("Sequence[Any]", value):
            found |= references_in(v)
    return found


def _is_reference(value: str) -> bool:
    return _REFERENCE.fullmatch(value) is not None


def _record_id(value: str) -> str:
    """A record id or a reference to one (kept as it is in the URL)."""
    if isinstance(value, str) and _is_reference(value):  # pyright: ignore[reportUnnecessaryIsInstance]
        return value
    return check_record_id(value)


def _encode_keep_refs(text: str) -> str:
    """Percent-encode ``text`` for a URL, but leave ``@{...}`` references as they are."""
    out: list[str] = []
    pos = 0
    for m in _REFERENCE.finditer(text):
        out.append(quote(text[pos : m.start()], safe=""))
        out.append(m.group(0))
        pos = m.end()
    out.append(quote(text[pos:], safe=""))
    return "".join(out)


def _check_path(path: str) -> str:
    if not isinstance(path, str) or not path:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("path must be a non-empty string")
    if path.startswith("/") or "://" in path:
        raise ValueError("pass a path relative to /services/data/vXX.X/, such as 'sobjects/Account'")
    if any(seg in ("..", ".") for seg in path.split("?", 1)[0].split("/")):
        raise ValueError("path traversal is not allowed")
    return path


def _kind_of(path: str) -> str:
    p = path.split("?", 1)[0].rstrip("/")
    first = p.split("/", 1)[0]
    if p == "composite/sobjects" or p.startswith("composite/sobjects/"):
        return "collections"
    if first in ("query", "queryAll"):
        return "query"
    return "other"


class Subrequests:
    """An ordered list of subrequests with the client-side limits of one request type.

    Every method adds one subrequest and returns its :class:`Reference`. Limits are checked
    when you add a subrequest, so the call that breaks a limit raises ``ValueError`` and
    nothing is sent.
    """

    _label: ClassVar[str] = "a Composite request"
    _max: ClassVar[int] = COMPOSITE_MAX
    _max_heavy: ClassVar[int | None] = COMPOSITE_MAX_HEAVY
    _allow_query: ClassVar[bool] = True
    _allow_refs: ClassVar[bool] = True

    def __init__(self) -> None:
        self._subs: list[Subrequest] = []

    def __len__(self) -> int:
        return len(self._subs)

    @property
    def subrequests(self) -> list[Subrequest]:
        """The subrequests added so far, in order."""
        return list(self._subs)

    # --- sObject rows -----------------------------------------------------------------

    def create(self, sobject: str, data: Mapping[str, Any], *, ref_id: str | None = None) -> Reference:
        """``POST sobjects/{sobject}``: create a record.

        Args:
            sobject: sObject API name.
            data: Field values. A value may be a reference to an earlier result.
            ref_id: Reference id for this subrequest; generated when omitted.
        """
        check_sobject(sobject)
        return self._add("POST", f"sobjects/{sobject}", ref_id, dict(data), "create", sobject, {})

    def update(
        self, sobject: str, record_id: str, data: Mapping[str, Any], *, ref_id: str | None = None
    ) -> Reference:
        """``PATCH sobjects/{sobject}/{id}``: update a record. ``record_id`` may be a reference."""
        check_sobject(sobject)
        rid = _record_id(record_id)
        return self._add(
            "PATCH", f"sobjects/{sobject}/{rid}", ref_id, dict(data), "update", sobject, {"id": rid}
        )

    def upsert(
        self,
        sobject: str,
        field: str,
        value: str,
        data: Mapping[str, Any],
        *,
        ref_id: str | None = None,
    ) -> Reference:
        """``PATCH sobjects/{sobject}/{field}/{value}``: upsert a record by external id."""
        check_sobject(sobject)
        soql_identifier(field)
        seg = value if _is_reference(str(value)) else url_segment(str(value))
        return self._add(
            "PATCH",
            f"sobjects/{sobject}/{field}/{seg}",
            ref_id,
            dict(data),
            "upsert",
            sobject,
            {"field": field, "value": str(value)},
        )

    def delete(self, sobject: str, record_id: str, *, ref_id: str | None = None) -> Reference:
        """``DELETE sobjects/{sobject}/{id}``: delete a record. ``record_id`` may be a reference."""
        check_sobject(sobject)
        rid = _record_id(record_id)
        return self._add("DELETE", f"sobjects/{sobject}/{rid}", ref_id, None, "delete", sobject, {"id": rid})

    def get(
        self,
        sobject: str,
        record_id: str,
        fields: Sequence[str] | None = None,
        *,
        ref_id: str | None = None,
    ) -> Reference:
        """``GET sobjects/{sobject}/{id}``: read a record, optionally only some ``fields``."""
        check_sobject(sobject)
        rid = _record_id(record_id)
        field_list = [soql_identifier(f) for f in fields] if fields else []
        path = f"sobjects/{sobject}/{rid}" + (f"?fields={','.join(field_list)}" if field_list else "")
        return self._add("GET", path, ref_id, None, "get", sobject, {"id": rid, "fields": field_list})

    def query(self, soql: str, *values: Any, ref_id: str | None = None) -> Reference:
        """``GET query?q=``: run a SOQL query (the first page of results).

        Positional values fill ``{}`` placeholders with the same quoting as ``sf.query``. A
        reference passed as a value becomes a quoted literal: ``WHERE AccountId = {}``.
        """
        if not self._allow_query:
            raise ValueError(f"{self._label} can't contain query subrequests")
        text = soql_values(soql, values)
        return self._add(
            "GET", f"query?q={_encode_keep_refs(text)}", ref_id, None, "query", None, {"soql": text}
        )

    def request(self, method: str, path: str, *, body: Any = None, ref_id: str | None = None) -> Reference:
        """Any other REST resource the request type supports, such as sObject Collections.

        Args:
            method: ``GET``, ``POST``, ``PATCH``, ``PUT`` or ``DELETE``.
            path: Path relative to ``/services/data/vXX.X/``, e.g. ``"composite/sobjects"``.
            body: JSON body (not for ``GET``).
            ref_id: Reference id for this subrequest; generated when omitted.
        """
        m = method.upper()
        if m not in _METHODS:
            raise ValueError(f"unsupported subrequest method: {method!r}")
        if m == "GET" and body is not None:
            raise ValueError("GET subrequests can't carry a body")
        _check_path(path)
        if _kind_of(path) == "query" and not self._allow_query:
            raise ValueError(f"{self._label} can't contain query subrequests")
        return self._add(m, path, ref_id, body, "request", None, {})

    # --- internals ----------------------------------------------------------------------

    def _new_ref_id(self, prefix: str) -> str:
        taken = {s.reference_id for s in self._subs}
        n = len(self._subs) + 1
        while f"{prefix}{n}" in taken:
            n += 1
        return f"{prefix}{n}"

    def _add(
        self,
        method: str,
        path: str,
        ref_id: str | None,
        body: Any,
        action: str,
        sobject: str | None,
        args: Mapping[str, Any],
    ) -> Reference:
        if len(self._subs) >= self._max:
            raise ValueError(f"{self._label} takes at most {self._max} subrequests")
        kind = "query" if action == "query" else ("sobject" if action != "request" else _kind_of(path))
        if self._max_heavy is not None and kind in ("collections", "query"):
            heavy = sum(1 for s in self._subs if s.kind in ("collections", "query"))
            if heavy >= self._max_heavy:
                raise ValueError(
                    f"at most {self._max_heavy} of the {self._max} subrequests of {self._label} "
                    "may be sObject Collections or query operations"
                )
        if ref_id is None:
            ref_id = self._new_ref_id(sobject or action)
        elif not isinstance(ref_id, str) or not _NAME.fullmatch(ref_id):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError(
                f"invalid ref_id {ref_id!r}: use letters, digits and underscores, starting with a letter"
            )
        if any(s.reference_id == ref_id for s in self._subs):
            raise ValueError(f"duplicate ref_id {ref_id!r}")
        used = references_in(path) | references_in(body)
        if used and not self._allow_refs:
            raise ValueError(f"{self._label} can't use references: its subrequests are independent")
        earlier = {s.reference_id for s in self._subs}
        unknown = sorted(used - earlier)
        if unknown:
            raise ValueError(
                f"reference to {unknown[0]!r}, which isn't an earlier subrequest of {self._label}"
            )
        self._subs.append(Subrequest(method, path, ref_id, body, kind, action, sobject, dict(args)))
        return Reference(ref_id)

    def _check_ready(self) -> None:
        if not self._subs:
            raise ValueError(f"add at least one subrequest to {self._label}")


class CompositeRequest(Subrequests):
    """Up to 25 subrequests that run in order; later ones can use earlier results.

    At most 5 of them may be sObject Collections or query operations.

    Attributes:
        all_or_none: Roll back every subrequest if one fails.
        collate_subrequests: Let Salesforce group independent subrequests to run them faster.
    """

    def __init__(self, *, all_or_none: bool = False, collate_subrequests: bool = False) -> None:
        super().__init__()
        self.all_or_none = all_or_none
        self.collate_subrequests = collate_subrequests


class Graph(Subrequests):
    """One graph of a Composite Graph request: up to 500 subrequests in one transaction.

    If one subrequest fails, Salesforce rolls back this graph only. Graphs can't contain
    query subrequests.

    Attributes:
        graph_id: The graph's id.
    """

    _label = "a graph"
    _max = GRAPH_MAX
    _max_heavy = None
    _allow_query = False

    def __init__(self, graph_id: str) -> None:
        super().__init__()
        self.graph_id = graph_id


class GraphRequest:
    """The graphs of one Composite Graph request."""

    def __init__(self) -> None:
        self._graphs: list[Graph] = []

    @property
    def graphs(self) -> list[Graph]:
        """The graphs added so far, in order."""
        return list(self._graphs)

    def graph(self, graph_id: str | None = None) -> Graph:
        """Add a graph and return it; add subrequests to the graph.

        Args:
            graph_id: Id for the graph (letters, digits and underscores); generated when omitted.
        """
        taken = {g.graph_id for g in self._graphs}
        if graph_id is None:
            n = len(self._graphs) + 1
            while f"graph{n}" in taken:
                n += 1
            graph_id = f"graph{n}"
        elif not isinstance(graph_id, str) or not _NAME.fullmatch(graph_id):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError(
                f"invalid graph_id {graph_id!r}: use letters, digits and underscores, starting with a letter"
            )
        if graph_id in taken:
            raise ValueError(f"duplicate graph_id {graph_id!r}")
        g = Graph(graph_id)
        self._graphs.append(g)
        return g

    def _check_ready(self) -> None:
        if not self._graphs:
            raise ValueError("add at least one graph to the Composite Graph request")
        for g in self._graphs:
            g._check_ready()  # pyright: ignore[reportPrivateUsage]


class BatchRequest(Subrequests):
    """Up to 25 independent subrequests. Each one is its own transaction; no references.

    Attributes:
        halt_on_error: Stop at the first failed subrequest; Salesforce skips the rest.
    """

    _label = "a Batch request"
    _max = BATCH_MAX
    _max_heavy = None
    _allow_refs = False

    def __init__(self, *, halt_on_error: bool = False) -> None:
        super().__init__()
        self.halt_on_error = halt_on_error


# --- payloads -------------------------------------------------------------------------------


def _composite_items(subs: Sequence[Subrequest], api_version: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for s in subs:
        item: dict[str, Any] = {
            "method": s.method,
            "url": f"/services/data/v{api_version}/{s.path}",
            "referenceId": s.reference_id,
        }
        if s.body is not None:
            item["body"] = s.body
        items.append(item)
    return items


def composite_payload(req: CompositeRequest, api_version: str) -> dict[str, Any]:
    """Body of ``POST composite``."""
    req._check_ready()  # pyright: ignore[reportPrivateUsage]
    return {
        "allOrNone": req.all_or_none,
        "collateSubrequests": req.collate_subrequests,
        "compositeRequest": _composite_items(req.subrequests, api_version),
    }


def graph_payload(req: GraphRequest, api_version: str) -> dict[str, Any]:
    """Body of ``POST composite/graph``."""
    req._check_ready()  # pyright: ignore[reportPrivateUsage]
    return {
        "graphs": [
            {"graphId": g.graph_id, "compositeRequest": _composite_items(g.subrequests, api_version)}
            for g in req.graphs
        ]
    }


def batch_payload(req: BatchRequest, api_version: str) -> dict[str, Any]:
    """Body of ``POST composite/batch``. Batch URLs start at the version: ``vXX.X/...``."""
    req._check_ready()  # pyright: ignore[reportPrivateUsage]
    items: list[dict[str, Any]] = []
    for s in req.subrequests:
        item: dict[str, Any] = {"method": s.method, "url": f"v{api_version}/{s.path}"}
        if s.body is not None:
            item["richInput"] = s.body
        items.append(item)
    return {"batchRequests": items, "haltOnError": req.halt_on_error}


def is_read_only(subs: Iterable[Subrequest]) -> bool:
    """Whether every subrequest is a GET, so resending the request is safe (§7.4)."""
    return all(s.method == "GET" for s in subs)


def _tree_refs(records: Sequence[Any], taken: set[str]) -> None:
    for rec in records:
        if not isinstance(rec, Mapping):
            raise TypeError("each tree record must be a dict")
        row = cast("Mapping[str, Any]", rec)
        attrs: Any = row.get("attributes")
        if isinstance(attrs, Mapping):
            ref: Any = cast("Mapping[str, Any]", attrs).get("referenceId")
            if ref is not None:
                if not isinstance(ref, str) or not _NAME.fullmatch(ref):
                    raise ValueError(
                        f"invalid referenceId {ref!r}: use letters, digits and underscores, "
                        "starting with a letter"
                    )
                if ref in taken:
                    raise ValueError(f"duplicate referenceId {ref!r}")
                taken.add(ref)
        for value in row.values():
            children = _children(value)
            if children is not None:
                _tree_refs(children, taken)


def _children(value: Any) -> list[Any] | None:
    if isinstance(value, Mapping):
        recs: Any = cast("Mapping[str, Any]", value).get("records")
        if isinstance(recs, list):
            return cast("list[Any]", recs)
    return None


def tree_payload(sobject: str, records: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Body of ``POST composite/tree/{sobject}`` and the reference ids, depth first.

    Root records get ``attributes.type = sobject``. Child records (a field whose value is
    ``{"records": [...]}``) need their own ``attributes.type``. Records without
    ``attributes.referenceId`` get ``ref1``, ``ref2``, ...

    Raises:
        ValueError: more than 200 records, 5 types or 5 levels; a root of another type; a
            child without a type; an invalid or duplicate reference id.
    """
    check_sobject(sobject)
    roots = list(records)
    if not roots:
        raise ValueError("pass at least one record")
    taken: set[str] = set()
    _tree_refs(roots, taken)
    refs: list[str] = []
    types: set[str] = set()
    counter = [0]

    def new_ref() -> str:
        while True:
            counter[0] += 1
            candidate = f"ref{counter[0]}"
            if candidate not in taken:
                taken.add(candidate)
                return candidate

    def walk(row: Mapping[str, Any], level: int, default_type: str | None) -> dict[str, Any]:
        if level > TREE_MAX_DEPTH:
            raise ValueError(f"an sObject Tree can be at most {TREE_MAX_DEPTH} levels deep")
        attrs_raw: Any = row.get("attributes")
        attrs: dict[str, Any] = (
            dict(cast("Mapping[str, Any]", attrs_raw)) if isinstance(attrs_raw, Mapping) else {}
        )
        rtype: Any = attrs.get("type") or default_type
        if not rtype:
            raise ValueError("child records in an sObject Tree need attributes.type")
        check_sobject(str(rtype))
        if level == 1 and rtype != sobject:
            raise ValueError(f"root records must be {sobject} records, got {rtype}")
        types.add(str(rtype))
        ref = attrs.get("referenceId") or new_ref()
        refs.append(str(ref))
        attrs["type"] = rtype
        attrs["referenceId"] = ref
        out: dict[str, Any] = {"attributes": attrs}
        for key, value in row.items():
            if key == "attributes":
                continue
            children = _children(value)
            if children is not None:
                out[key] = {
                    "records": [walk(cast("Mapping[str, Any]", c), level + 1, None) for c in children]
                }
            else:
                out[key] = value
        return out

    body = {"records": [walk(r, 1, sobject) for r in roots]}
    if len(refs) > TREE_MAX_RECORDS:
        raise ValueError(f"an sObject Tree request takes at most {TREE_MAX_RECORDS} records, got {len(refs)}")
    if len(types) > TREE_MAX_TYPES:
        raise ValueError(
            f"an sObject Tree request takes at most {TREE_MAX_TYPES} record types, got {len(types)}"
        )
    return body, refs


# --- results --------------------------------------------------------------------------------


def _with_ref(details: Iterable[ErrorDetail], ref: str | None) -> tuple[ErrorDetail, ...]:
    if ref is None:
        return tuple(details)
    return tuple(
        ErrorDetail(d.error_code, d.message, d.fields, {**d.extended, "referenceId": ref}) for d in details
    )


def subrequest_error(
    sub: Subrequest,
    status: int,
    body: Any,
    *,
    operation: str,
    error_tags: Mapping[str, type[SalesforceError]] | None = None,
    reference_id: str | None = None,
) -> SalesforceError:
    """The typed error (§8) for a failed subrequest, from its status and error entries."""
    details = _with_ref(parse_details(body), reference_id)
    if not details:
        details = _with_ref((ErrorDetail(None, f"HTTP {status}"),), reference_id)
    spec = RequestSpec(
        sub.method, sub.path, operation=operation, sobject=sub.sobject, dml=sub.method != "GET"
    )
    return error_from_detail(
        details[0], status=status, spec=spec, dml=spec.dml, error_tags=error_tags, errors=details
    )


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in cast("Mapping[Any, Any]", value).items()}


def _sub_results(
    items: Any,
    subs: Sequence[Subrequest],
    *,
    operation: str,
    error_tags: Mapping[str, type[SalesforceError]] | None,
) -> CompositeResult:
    """Match ``compositeResponse`` entries to the subrequests by ``referenceId``."""
    if not isinstance(items, list) or len(cast("list[Any]", items)) != len(subs):
        raise UnexpectedResponse(
            f"expected {len(subs)} subrequest results",
            operation=operation,
            raw=repr(cast(object, items))[:4096],
        )
    by_ref: dict[str, Mapping[str, Any]] = {}
    for raw_item in cast("list[Any]", items):
        if not isinstance(raw_item, Mapping):
            raise UnexpectedResponse(
                "malformed subrequest result", operation=operation, raw=repr(cast(object, raw_item))[:4096]
            )
        item = cast("Mapping[str, Any]", raw_item)
        by_ref[str(item.get("referenceId"))] = item
    out: list[SubrequestResult] = []
    for sub in subs:
        found = by_ref.get(sub.reference_id)
        if found is None:
            raise UnexpectedResponse(f"no result for subrequest {sub.reference_id!r}", operation=operation)
        out.append(
            _one_result(
                sub,
                found.get("httpStatusCode"),
                found.get("body"),
                found.get("httpHeaders"),
                operation,
                error_tags,
                sub.reference_id,
            )
        )
    return CompositeResult(out, operation=operation)


def _one_result(
    sub: Subrequest,
    status_raw: Any,
    body: Any,
    headers: Any,
    operation: str,
    error_tags: Mapping[str, type[SalesforceError]] | None,
    reference_id: str | None,
) -> SubrequestResult:
    if not isinstance(status_raw, int) or isinstance(status_raw, bool):
        raise UnexpectedResponse("subrequest result has no HTTP status", operation=operation)
    error = None
    if not 200 <= status_raw < 300:
        error = subrequest_error(
            sub, status_raw, body, operation=operation, error_tags=error_tags, reference_id=reference_id
        )
    return SubrequestResult(reference_id, status_raw, body, _headers(headers), error)


def _mapping(body: Any, key: str, operation: str) -> Any:
    if not isinstance(body, Mapping) or key not in body:
        raise UnexpectedResponse(
            f"response has no {key!r}", operation=operation, raw=repr(cast(object, body))[:4096]
        )
    return cast("Mapping[str, Any]", body)[key]


def parse_composite(
    body: Any, req: CompositeRequest, *, error_tags: Mapping[str, type[SalesforceError]] | None = None
) -> CompositeResult:
    """Parse ``{"compositeResponse": [...]}`` into a :class:`CompositeResult` in request order."""
    op = _SPEC_OPERATION["composite"]
    return _sub_results(
        _mapping(body, "compositeResponse", op), req.subrequests, operation=op, error_tags=error_tags
    )


def parse_graphs(
    body: Any, req: GraphRequest, *, error_tags: Mapping[str, type[SalesforceError]] | None = None
) -> list[GraphResult]:
    """Parse ``{"graphs": [{"graphId", "graphResponse", "isSuccessful"}]}``, in request order."""
    op = _SPEC_OPERATION["graph"]
    items = _mapping(body, "graphs", op)
    if not isinstance(items, list):
        raise UnexpectedResponse("'graphs' is not a list", operation=op)
    by_id: dict[str, Mapping[str, Any]] = {}
    for raw_item in cast("list[Any]", items):
        if isinstance(raw_item, Mapping):
            item = cast("Mapping[str, Any]", raw_item)
            by_id[str(item.get("graphId"))] = item
    out: list[GraphResult] = []
    for g in req.graphs:
        found = by_id.get(g.graph_id)
        if found is None:
            raise UnexpectedResponse(f"no result for graph {g.graph_id!r}", operation=op)
        subs = _sub_results(
            _mapping(found.get("graphResponse"), "compositeResponse", op),
            g.subrequests,
            operation=op,
            error_tags=error_tags,
        )
        out.append(GraphResult(g.graph_id, found.get("isSuccessful") is True, subs))
    return out


def parse_batch(
    body: Any, req: BatchRequest, *, error_tags: Mapping[str, type[SalesforceError]] | None = None
) -> CompositeResult:
    """Parse ``{"hasErrors", "results": [{"statusCode", "result"}]}``; results keep request order."""
    op = _SPEC_OPERATION["batch"]
    items = _mapping(body, "results", op)
    subs = req.subrequests
    if not isinstance(items, list) or len(cast("list[Any]", items)) != len(subs):
        raise UnexpectedResponse(f"expected {len(subs)} batch results", operation=op)
    out: list[SubrequestResult] = []
    for sub, raw_item in zip(subs, cast("list[Any]", items), strict=True):
        if not isinstance(raw_item, Mapping):
            raise UnexpectedResponse("malformed batch result", operation=op)
        item = cast("Mapping[str, Any]", raw_item)
        out.append(
            _one_result(
                sub, item.get("statusCode"), item.get("result"), None, op, error_tags, sub.reference_id
            )
        )
    return CompositeResult(out, operation=op)


def parse_tree(
    status: int,
    body: Any,
    *,
    spec: RequestSpec,
    error_tags: Mapping[str, type[SalesforceError]] | None = None,
) -> dict[str, str]:
    """Parse an sObject Tree response into ``{referenceId: id}``, or raise its typed error.

    The request is atomic: when ``hasErrors`` is true nothing was saved, and the error of the
    first failed record picks the class. ``.errors`` holds every entry, each with
    ``extended["referenceId"]``.
    """
    results = _mapping(body, "results", spec.operation)
    if not isinstance(results, list):
        raise UnexpectedResponse("'results' is not a list", operation=spec.operation)
    rows = [cast("Mapping[str, Any]", r) for r in cast("list[Any]", results) if isinstance(r, Mapping)]
    if cast("Mapping[str, Any]", body).get("hasErrors"):
        details: list[ErrorDetail] = []
        for r in rows:
            ref = r.get("referenceId")
            details.extend(
                _with_ref(parse_details(r.get("errors") or []), str(ref) if ref is not None else None)
            )
        if not details:
            raise UnexpectedResponse(
                "sObject Tree request failed without error details", operation=spec.operation
            )
        return_error = error_from_detail(
            details[0], status=status, spec=spec, dml=True, error_tags=error_tags, errors=tuple(details)
        )
        raise return_error
    return {str(r.get("referenceId")): str(r.get("id")) for r in rows}
