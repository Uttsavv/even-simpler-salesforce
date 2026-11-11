"""Composite family: ``sf.composite`` (§9.4)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from ...core.composite import (
    BatchRequest,
    CompositeRequest,
    GraphRequest,
    batch_payload,
    composite_payload,
    graph_payload,
    is_read_only,
    parse_batch,
    parse_composite,
    parse_graphs,
    parse_tree,
    tree_payload,
)
from ...core.http import RequestSpec
from ...models import CompositeResult, GraphResult

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["AsyncBatchBuilder", "AsyncComposite", "AsyncCompositeBuilder", "AsyncGraphBuilder"]


async def _write(
    sf: AsyncSalesforce, path: str, payload: Any, operation: str, records: int, read_only: bool
) -> Any:
    content, ctype = sf._transport.encode(payload)
    raw = await sf._send(
        RequestSpec(
            "POST",
            path,
            content=content,
            content_type=ctype,
            # A request with any write is never resent once it may have reached Salesforce
            # (ADR 0001); one made only of GETs is a read.
            idempotent=True if read_only else None,
            operation=operation,
            records=records,
            compress=True,
            dml=not read_only,
        )
    )
    return raw.json()


class AsyncCompositeBuilder(CompositeRequest):
    """Builds one Composite request. Get one with ``sf.composite.builder()``.

    Add up to 25 subrequests with ``create``, ``update``, ``upsert``, ``delete``, ``get``,
    ``query`` and ``request``. Each returns a :class:`~even_simpler_salesforce.Reference`;
    pass ``ref.ref("id")`` to a later subrequest to use the earlier result. Then call
    :meth:`send`.
    """

    def __init__(self, client: AsyncSalesforce, *, all_or_none: bool, collate_subrequests: bool) -> None:
        super().__init__(all_or_none=all_or_none, collate_subrequests=collate_subrequests)
        self._sf = client

    async def send(self) -> CompositeResult:
        """``POST composite``: run the subrequests in one round trip.

        Returns:
            One result per subrequest, in the order you added them. A failed subrequest doesn't
            raise; call ``raise_for_errors()``.

        Raises:
            ValueError: no subrequests were added.
        """
        payload = composite_payload(self, self._sf._transport.api_version)
        body = await _write(
            self._sf, "composite", payload, "composite.send", len(self), is_read_only(self.subrequests)
        )
        return parse_composite(body, self, error_tags=self._sf._transport.error_tags)


class AsyncGraphBuilder(GraphRequest):
    """Builds one Composite Graph request. Get one with ``sf.composite.graph()``.

    Call :meth:`graph` once per graph and add subrequests to each graph. Each graph runs in
    its own transaction: if one of its subrequests fails, Salesforce rolls back that graph
    only.
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        super().__init__()
        self._sf = client

    async def send(self) -> list[GraphResult]:
        """``POST composite/graph``: run every graph in one round trip.

        Returns:
            One :class:`~even_simpler_salesforce.GraphResult` per graph, in the order you added
            them. A failed graph doesn't raise; check ``.ok`` or call ``raise_for_errors()``.

        Raises:
            ValueError: no graphs were added, or a graph has no subrequests.
        """
        payload = graph_payload(self, self._sf._transport.api_version)
        subs = [s for g in self.graphs for s in g.subrequests]
        body = await _write(
            self._sf, "composite/graph", payload, "composite.graph", len(subs), is_read_only(subs)
        )
        return parse_graphs(body, self, error_tags=self._sf._transport.error_tags)


class AsyncBatchBuilder(BatchRequest):
    """Builds one Batch request. Get one with ``sf.composite.batch()``.

    Add up to 25 independent subrequests. Each one runs in its own transaction, and they
    can't use each other's results.
    """

    def __init__(self, client: AsyncSalesforce, *, halt_on_error: bool) -> None:
        super().__init__(halt_on_error=halt_on_error)
        self._sf = client

    async def send(self) -> CompositeResult:
        """``POST composite/batch``: run the subrequests in one round trip.

        Returns:
            One result per subrequest, in the order you added them. Index it by position or by
            the reference id the builder returned.

        Raises:
            ValueError: no subrequests were added.
        """
        payload = batch_payload(self, self._sf._transport.api_version)
        body = await _write(
            self._sf, "composite/batch", payload, "composite.batch", len(self), is_read_only(self.subrequests)
        )
        return parse_batch(body, self, error_tags=self._sf._transport.error_tags)


class AsyncComposite:
    """Many REST calls in one round trip: Composite, Composite Graph, sObject Tree and Batch.

    Every limit is checked before the client sends anything, with a ``ValueError`` that names
    the limit. A request that writes is never resent after it may have reached Salesforce.
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client

    def builder(
        self, *, all_or_none: bool = False, collate_subrequests: bool = False
    ) -> AsyncCompositeBuilder:
        """A Composite request: up to 25 subrequests that can use each other's results.

        Args:
            all_or_none: Roll back every subrequest if one fails.
            collate_subrequests: Let Salesforce group independent subrequests to run them
                faster. The results still come back in the order you added them.
        """
        return AsyncCompositeBuilder(
            self._sf, all_or_none=all_or_none, collate_subrequests=collate_subrequests
        )

    def graph(self) -> AsyncGraphBuilder:
        """A Composite Graph request: graphs of up to 500 subrequests, each graph atomic."""
        return AsyncGraphBuilder(self._sf)

    def batch(self, *, halt_on_error: bool = False) -> AsyncBatchBuilder:
        """A Batch request: up to 25 independent subrequests.

        Args:
            halt_on_error: Skip the remaining subrequests after the first one fails.
        """
        return AsyncBatchBuilder(self._sf, halt_on_error=halt_on_error)

    async def tree(self, sobject: str, records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
        """``POST composite/tree/{sobject}``: create record trees in one transaction.

        Child records go in a child relationship field as ``{"records": [...]}`` and need
        ``attributes.type``. Give a record ``attributes.referenceId`` to find its id in the
        result; records without one get ``ref1``, ``ref2``, ... in depth-first order.

        Args:
            sobject: sObject type of the root records.
            records: Root records, with their children nested inside.

        Returns:
            ``{referenceId: id}`` for every record created.

        Raises:
            ValueError: more than 200 records, 5 record types or 5 levels, before anything is
                sent.
            RecordError: Salesforce rejected a record; nothing was saved. The error of the
                first rejected record picks the class (for example ``RequiredFieldMissing``),
                and ``.errors`` holds every entry with ``extended["referenceId"]``.
        """
        payload, refs = tree_payload(sobject, records)
        content, ctype = self._sf._transport.encode(payload)
        spec = RequestSpec(
            "POST",
            f"composite/tree/{sobject}",
            content=content,
            content_type=ctype,
            operation="composite.tree",
            sobject=sobject,
            records=len(refs),
            compress=True,
            dml=True,
        )
        raw = await self._sf._send(spec)
        return parse_tree(raw.status, raw.json(), spec=spec, error_tags=self._sf._transport.error_tags)
