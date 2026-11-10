"""GraphQL API (§9.6): request bodies, document scanning, errors and paging. No I/O.

Salesforce serves GraphQL at ``POST /services/data/vXX.X/graphql``. A failed query can still
answer ``200 OK``: the body then holds an ``errors`` array, sometimes next to partial ``data``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from ..errors import ErrorDetail, GraphQLError, UnexpectedResponse
from .http import RawResponse, RequestSpec, path_template

__all__ = [
    "CURSOR_VARIABLE",
    "GRAPHQL_PATH",
    "Operation",
    "check_cursor_query",
    "connection_page",
    "graphql_body",
    "graphql_error",
    "graphql_sobject",
    "has_mutation",
    "parse_graphql_data",
    "parse_path",
    "scan_operations",
]

GRAPHQL_PATH = "graphql"
"""Path of the GraphQL endpoint, relative to ``/services/data/vXX.X/``."""

CURSOR_VARIABLE = "after"
"""Variable that ``graphql_iter`` sets to ``pageInfo.endCursor`` for the next page."""

_RAW_LIMIT = 4096
_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_PATH = re.compile(r"^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$")
_KINDS = frozenset({"query", "mutation", "subscription"})


@dataclass(frozen=True, slots=True)
class Operation:
    """One operation definition of a GraphQL document.

    Attributes:
        kind: ``"query"``, ``"mutation"`` or ``"subscription"``. A shorthand ``{ ... }``
            document is a ``"query"``.
        name: Operation name, if the document gives one.
        variables: Names of the declared variables, without the ``$``.
    """

    kind: str
    name: str | None
    variables: tuple[str, ...]


def _tokens(document: str) -> list[str]:
    """Significant tokens: names, ``$name`` and punctuation. Strings and comments are dropped."""
    out: list[str] = []
    i, n = 0, len(document)
    while i < n:
        ch = document[i]
        if ch in " \t\r\n,﻿":
            i += 1
        elif ch == "#":
            end = document.find("\n", i)
            i = n if end < 0 else end + 1
        elif document.startswith('"""', i):
            end = document.find('"""', i + 3)
            while end > 0 and document[end - 1] == "\\":
                end = document.find('"""', end + 3)
            i = n if end < 0 else end + 3
        elif ch == '"':
            j = i + 1
            while j < n and document[j] not in '"\n':
                j += 2 if document[j] == "\\" else 1
            i = j + 1
        elif ch == "$":
            m = _NAME.match(document, i + 1)
            out.append("$" + m.group(0) if m else "$")
            i = m.end() if m else i + 1
        else:
            m = _NAME.match(document, i)
            if m:
                out.append(m.group(0))
                i = m.end()
            else:
                out.append(ch)
                i += 1
    return out


def scan_operations(document: str) -> list[Operation]:
    """The operations a GraphQL document defines, in order (fragments are skipped).

    This is a light scan, not a GraphQL parser: Salesforce validates the document. It reads
    the operation kind and the declared variables, which the client needs to decide whether
    the call is a read and whether it can be paged. A malformed document never raises here;
    Salesforce rejects it with a ``GraphQLError``.
    """
    toks = _tokens(document)
    ops: list[Operation] = []
    depth = 0
    i = 0
    while i < len(toks):
        tok = toks[i]
        if depth == 0 and tok in _KINDS:
            name: str | None = None
            j = i + 1
            if j < len(toks) and _NAME.fullmatch(toks[j]):
                name = toks[j]
                j += 1
            variables: list[str] = []
            if j < len(toks) and toks[j] == "(":
                level = 0
                while j < len(toks):
                    t = toks[j]
                    level += t == "("
                    level -= t == ")"
                    if level == 1 and len(t) > 1 and t[0] == "$" and j + 1 < len(toks) and toks[j + 1] == ":":
                        variables.append(t[1:])
                    j += 1
                    if level == 0:
                        break
            ops.append(Operation(tok, name, tuple(variables)))
            i = j
            continue
        if depth == 0 and tok == "{" and _closed_before(toks, i):
            ops.append(Operation("query", None, ()))
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth = max(0, depth - 1)
        i += 1
    return ops


def _closed_before(toks: Sequence[str], i: int) -> bool:
    """Whether the ``{`` at ``i`` starts a shorthand query (the previous definition ended)."""
    return i == 0 or toks[i - 1] == "}"


def has_mutation(document: str) -> bool:
    """Whether the document defines a mutation (the call then writes and is never resent)."""
    return any(op.kind == "mutation" for op in scan_operations(document))


def check_cursor_query(document: str) -> Operation:
    """Check that a document can be paged by ``graphql_iter``.

    Returns:
        The single query operation.

    Raises:
        ValueError: the document is not exactly one query, or the query doesn't declare an
            ``$after`` variable.
    """
    ops = scan_operations(document)
    if len(ops) != 1 or ops[0].kind != "query":
        raise ValueError("graphql_iter needs a document with exactly one query operation")
    if CURSOR_VARIABLE not in ops[0].variables:
        raise ValueError(
            "graphql_iter needs a query that declares an $after variable, for example "
            "query Leads($after: String) { uiapi { query { Lead(first: 200, after: $after) { ... } } } }"
        )
    return ops[0]


def graphql_body(query: str, variables: Mapping[str, Any] | None) -> dict[str, Any]:
    """The JSON body of a GraphQL request.

    Raises:
        ValueError: ``query`` is empty.
        TypeError: ``query`` is not a string, or ``variables`` is not a mapping.
    """
    if not isinstance(query, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("query must be a string")
    if not query.strip():
        raise ValueError("query must not be empty")
    body: dict[str, Any] = {"query": query}
    if variables is not None:
        if not isinstance(variables, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("variables must be a mapping")
        body["variables"] = dict(variables)
    return body


def parse_path(path: str) -> tuple[str, ...]:
    """Split a dotted ``path`` such as ``"uiapi.query.Lead"`` into its field names.

    Raises:
        ValueError: ``path`` is not dot-separated GraphQL names.
    """
    if not isinstance(path, str) or not _PATH.fullmatch(path):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"path must be dot-separated field names such as 'uiapi.query.Lead', got {path!r}")
    return tuple(path.split("."))


def graphql_sobject(path: Sequence[str]) -> str | None:
    """The sObject of a ``uiapi.query.<Object>`` path, for logs; ``None`` otherwise."""
    if len(path) >= 3 and path[0] == "uiapi" and path[1] == "query":
        return path[2]
    return None


def _detail(item: Mapping[str, Any]) -> ErrorDetail:
    extensions: Any = item.get("extensions")
    code: Any = (
        cast("Mapping[str, Any]", extensions).get("ErrorType") if isinstance(extensions, Mapping) else None
    )
    extended = {k: item[k] for k in ("path", "locations", "extensions") if k in item}
    return ErrorDetail(
        error_code=str(code) if code is not None else None,
        message=str(item.get("message") or ""),
        extended=extended,
    )


def graphql_error(resp: RawResponse, spec: RequestSpec) -> GraphQLError | None:
    """A :class:`GraphQLError` when the body is a GraphQL response with ``errors``, else ``None``.

    Used by the transport's classifier for every status, so a ``200 OK`` that carries errors
    fails inside the pipeline. Bodies that aren't GraphQL responses (a REST error list, a
    proxy page) return ``None`` and are classified as usual.
    """
    try:
        body: Any = resp.json()
    except ValueError:
        return None
    if not isinstance(body, Mapping):
        return None
    raw_errors: Any = cast("Mapping[str, Any]", body).get("errors")
    if not isinstance(raw_errors, list) or not raw_errors:
        return None
    items = [
        dict(cast("Mapping[str, Any]", e)) if isinstance(e, Mapping) else {"message": str(e)}
        for e in cast("list[Any]", raw_errors)
    ]
    details = tuple(_detail(e) for e in items)
    first = details[0]
    message = first.message or "GraphQL request failed"
    if len(details) > 1:
        message += f" (and {len(details) - 1} more)"
    return GraphQLError(
        message,
        graphql_errors=tuple(items),
        data=cast("Mapping[str, Any]", body).get("data"),
        error_code=first.error_code,
        errors=details,
        status=resp.status,
        operation=spec.operation,
        sobject=spec.sobject,
        method=spec.method,
        path=spec.path_template or path_template(spec.path),
        request_id=resp.request_id,
        raw=resp.content[:_RAW_LIMIT],
    )


def parse_graphql_data(body: Any) -> dict[str, Any]:
    """The ``data`` member of a successful GraphQL response.

    Raises:
        UnexpectedResponse: the body has no ``data`` object.
    """
    data: Any = cast("Mapping[str, Any]", body).get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, dict):
        raise UnexpectedResponse(
            "GraphQL response has no 'data' object", raw=repr(cast(object, body))[:_RAW_LIMIT]
        )
    return cast("dict[str, Any]", data)


def connection_page(data: Mapping[str, Any], path: Sequence[str]) -> tuple[list[Any], str | None]:
    """The nodes of the connection at ``path`` and the cursor of the next page.

    Returns:
        ``(nodes, cursor)``. ``cursor`` is ``pageInfo.endCursor`` when ``hasNextPage`` is
        true, else ``None``.

    Raises:
        UnexpectedResponse: ``path`` isn't in the data, or the connection lacks ``edges``
            or ``pageInfo { hasNextPage endCursor }``.
    """
    node: Any = data
    for i, key in enumerate(path):
        node = cast("Mapping[str, Any]", node).get(key) if isinstance(node, Mapping) else None
        if node is None:
            where = ".".join(path[: i + 1])
            raise UnexpectedResponse(f"GraphQL response has no {where!r}; check path=")
    if not isinstance(node, Mapping):
        raise UnexpectedResponse(f"GraphQL response field {'.'.join(path)!r} is not a connection")
    conn = cast("Mapping[str, Any]", node)
    edges: Any = conn.get("edges")
    info: Any = conn.get("pageInfo")
    if not isinstance(edges, list) or not isinstance(info, Mapping):
        raise UnexpectedResponse(
            f"GraphQL connection {'.'.join(path)!r} needs edges {{ node {{ ... }} }} and "
            "pageInfo { hasNextPage endCursor } in the query"
        )
    nodes: list[Any] = []
    for edge in cast("list[Any]", edges):
        if not isinstance(edge, Mapping) or "node" not in edge:
            raise UnexpectedResponse(f"GraphQL connection {'.'.join(path)!r} has an edge without 'node'")
        nodes.append(cast("Mapping[str, Any]", edge)["node"])
    page_info = cast("Mapping[str, Any]", info)
    if page_info.get("hasNextPage") is not True:
        return nodes, None
    cursor: Any = page_info.get("endCursor")
    if not isinstance(cursor, str) or not cursor:
        raise UnexpectedResponse(
            f"GraphQL connection {'.'.join(path)!r} has a next page but no endCursor; "
            "select pageInfo { hasNextPage endCursor }"
        )
    return nodes, cursor
