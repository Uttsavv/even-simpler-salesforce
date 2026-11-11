"""Fake of ``sf.graphql`` and ``sf.graphql_iter`` (§9.6, §14).

GraphQL is scripted, not executed, like SOQL: ``fake.on_graphql(match, data)`` answers a
document. With ``nodes=`` and ``path=`` the fake builds a connection and pages it by the
``after`` variable, so code that uses ``graphql_iter`` runs unchanged.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.graphql import (
    CURSOR_VARIABLE,
    GRAPHQL_PATH,
    check_cursor_query,
    connection_page,
    graphql_body,
    graphql_sobject,
    parse_path,
)
from ._state import FakeState, UnscriptedCall, Who

__all__ = ["GraphQLScript", "fake_graphql", "fake_graphql_iter"]

GraphQLMatch = str | re.Pattern[str] | Callable[[str], bool]
_CURSOR = "fake-cursor-"


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclass(slots=True)
class GraphQLScript:
    """A scripted answer for ``graphql`` and ``graphql_iter`` (returned by ``fake.on_graphql``).

    Attributes:
        match: The document (compared with whitespace collapsed), a compiled regex
            (``re.search``), or a predicate ``(query) -> bool``.
        data: The ``data`` member to return, a callable ``(query, variables) -> data``, or an
            exception instance to raise. Unused when ``nodes`` is set.
        nodes: Connection nodes, paged by the ``after`` variable.
        path: Dotted path from ``data`` to the connection that holds ``nodes``.
        page_size: Nodes per page when ``nodes`` is set.
        times: How many times the script may match (``None`` = unlimited).
        hits: How many times it matched so far.
    """

    match: GraphQLMatch
    data: Any = None
    nodes: Sequence[Any] | None = None
    path: tuple[str, ...] = ()
    page_size: int = 10
    times: int | None = None
    hits: int = 0

    def matches(self, query: str) -> bool:
        """Whether this script answers ``query``."""
        if self.times is not None and self.hits >= self.times:
            return False
        m = self.match
        if isinstance(m, str):
            return _norm(m) == _norm(query)
        if isinstance(m, re.Pattern):
            return m.search(query) is not None
        return bool(m(query))

    def answer(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        """The ``data`` member for one call."""
        if self.nodes is None:
            out: Any = self.data
            if isinstance(out, BaseException):
                raise copy.copy(out)
            if callable(out):
                out = out(query, copy.deepcopy(dict(variables)))
            return copy.deepcopy(dict(out or {}))
        return self._connection(variables.get(CURSOR_VARIABLE))

    def _connection(self, after: Any) -> dict[str, Any]:
        nodes = list(self.nodes or ())
        start = 0
        if after is not None:
            text = str(after)
            if not (text.startswith(_CURSOR) and text[len(_CURSOR) :].isdigit()):
                raise ValueError(f"after={after!r} is not a cursor this fake handed out")
            start = int(text[len(_CURSOR) :])
        end = start + self.page_size
        conn = {
            "edges": [{"node": copy.deepcopy(n)} for n in nodes[start:end]],
            "pageInfo": {
                "hasNextPage": end < len(nodes),
                "hasPreviousPage": start > 0,
                "startCursor": f"{_CURSOR}{start}" if start < len(nodes) else None,
                "endCursor": f"{_CURSOR}{min(end, len(nodes))}",
            },
            "totalCount": len(nodes),
        }
        data: dict[str, Any] = conn
        for key in reversed(self.path):
            data = {key: data}
        return data

    def describe(self) -> str:
        """Short human description, for error messages."""
        m = self.match
        if isinstance(m, str):
            return repr(_norm(m))
        if isinstance(m, re.Pattern):
            return f"re.compile({m.pattern!r})"
        return getattr(m, "__name__", repr(m))


def add_graphql_script(state: FakeState, script: GraphQLScript) -> GraphQLScript:
    if script.nodes is not None and not script.path:
        raise ValueError("on_graphql(nodes=...) needs path=, e.g. path='uiapi.query.Lead'")
    if script.page_size < 1:
        raise ValueError("page_size must be >= 1")
    with state.lock:
        state.graphql_scripts.insert(0, script)  # newest first
    return script


def _script_for(state: FakeState, query: str) -> GraphQLScript:
    for s in state.graphql_scripts:
        if s.matches(query):
            s.hits += 1
            return s
    known = [s.describe() for s in state.graphql_scripts]
    raise UnscriptedCall(
        f"no GraphQL script matches {_norm(query)!r}. Register one with fake.on_graphql(...)"
        + (f"; registered: {', '.join(known)}" if known else "; none registered")
    )


def fake_graphql(
    state: FakeState,
    who: Who,
    query: str,
    variables: Mapping[str, Any] | None,
    *,
    sobject: str | None = None,
) -> dict[str, Any]:
    """The scripted ``data`` member for ``query``, recorded as a ``graphql`` call."""
    body = graphql_body(query, variables)  # same validation as the client
    params: dict[str, Any] = body.get("variables", {})
    return state.run(
        who,
        "graphql",
        sobject,
        {"query": query, "variables": params},
        lambda: _script_for(state, query).answer(query, params),
        method="POST",
        path=GRAPHQL_PATH,
    )


def fake_graphql_iter(
    state: FakeState, who: Who, query: str, variables: Mapping[str, Any] | None, path: str
) -> Iterator[Any]:
    """Yield the nodes of the scripted connection at ``path``, one ``graphql`` call per page."""
    check_cursor_query(query)
    keys = parse_path(path)
    sobject = graphql_sobject(keys)
    params = dict(variables or {})
    while True:
        nodes, cursor = connection_page(fake_graphql(state, who, query, params, sobject=sobject), keys)
        yield from nodes
        if cursor is None:
            return
        params[CURSOR_VARIABLE] = cursor
