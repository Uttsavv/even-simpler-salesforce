"""GraphQL API: ``sf.graphql`` and ``sf.graphql_iter`` (§9.6)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

from ...core.graphql import (
    CURSOR_VARIABLE,
    check_cursor_query,
    connection_page,
    graphql_body,
    graphql_sobject,
    has_mutation,
    parse_graphql_data,
    parse_path,
)
from ...core.http import RequestSpec
from ...errors import UnexpectedResponse

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["graphql", "graphql_iter"]


async def graphql(
    sf: AsyncSalesforce,
    query: str,
    variables: Mapping[str, Any] | None,
    *,
    sobject: str | None = None,
) -> dict[str, Any]:
    """``POST graphql`` through the pipeline and return the ``data`` member.

    A query is a read, so it is resent after a timeout or a 5xx. A document with a mutation
    is never resent after it may have reached Salesforce.
    """
    content, ctype = sf._transport.encode(graphql_body(query, variables))  # pyright: ignore[reportPrivateUsage]
    raw = await sf._send(  # pyright: ignore[reportPrivateUsage]
        RequestSpec(
            "POST",
            "graphql",
            content=content,
            content_type=ctype,
            idempotent=not has_mutation(query),
            operation="graphql",
            sobject=sobject,
            compress=True,
        )
    )
    return parse_graphql_data(raw.json())


async def graphql_iter(
    sf: AsyncSalesforce,
    query: str,
    variables: Mapping[str, Any] | None,
    path: str,
) -> AsyncIterator[Any]:
    """Yield the edge nodes of the connection at ``path``, page by page."""
    check_cursor_query(query)
    keys = parse_path(path)
    sobject = graphql_sobject(keys)
    params = dict(variables or {})
    seen: set[str] = set()
    while True:
        data = await graphql(sf, query, params, sobject=sobject)
        nodes, cursor = connection_page(data, keys)
        for node in nodes:
            yield node
        if cursor is None:
            return
        if cursor in seen:
            raise UnexpectedResponse(f"GraphQL connection {path!r} returned the same endCursor twice")
        seen.add(cursor)
        params[CURSOR_VARIABLE] = cursor
