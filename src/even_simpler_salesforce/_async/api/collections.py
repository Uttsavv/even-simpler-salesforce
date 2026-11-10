"""sObject Collections: ``sf.collections`` (§9.3)."""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ...core.classify import error_from_detail
from ...core.http import RequestSpec, check_record_id, check_sobject
from ...core.records import (
    COLLECTION_READ_CHUNK,
    COLLECTION_WRITE_CHUNK,
    chunked,
    parse_record_results,
    with_type,
)
from ...core.soql import soql_identifier
from ...errors import (
    ConnectError,
    ErrorDetail,
    PartialFailure,
    RequestTimeout,
    SalesforceError,
    ServerError,
    TransportError,
    UnexpectedResponse,
)
from ...models import BatchResult, RecordResult
from .. import _compat

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["AsyncCollections"]

_log = logging.getLogger("esf")
_ROLLED_BACK = "ALL_OR_NONE_OPERATION_ROLLED_BACK"


def _chunk_failed(exc: SalesforceError) -> RecordResult:
    """Per-row result for a chunk whose whole request failed.

    ``extended["outcome"]`` is ``"unknown"`` when the request may have reached Salesforce
    (timeouts, resets, 5xx): those rows may or may not have been written.
    """
    reached = exc.status is not None and exc.status >= 500  # e.g. an HTML 502/503 proxy page
    unknown = (
        reached
        or isinstance(exc, (RequestTimeout, ServerError))
        or (isinstance(exc, TransportError) and not isinstance(exc, ConnectError) and exc.retryable)
    )
    detail = ErrorDetail(
        error_code=exc.error_code or type(exc).__name__,
        message=exc.message,
        extended={"chunk_error": type(exc).__name__, "outcome": "unknown" if unknown else "rejected"},
    )
    return RecordResult(ok=False, errors=(detail,))


class AsyncCollections:
    """Many records per round trip. Results keep the input order.

    With ``all_or_none=True`` each chunk of 200 is atomic, but chunks are separate
    transactions: when a later chunk fails, **earlier chunks are already committed**. Chunks
    then run one at a time and the first failing chunk raises a typed error.
    """

    def __init__(self, client: AsyncSalesforce, concurrency: int = 4) -> None:
        self._sf = client
        self.concurrency = concurrency

    # --- writes ------------------------------------------------------------------------

    async def create(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        sobject: str | None = None,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """``POST composite/sobjects`` in chunks of 200. Rows need ``attributes.type`` or ``sobject=``."""
        name = check_sobject(sobject) if sobject else None
        typed = [with_type(r, name) for r in rows]
        return await self._write(
            "POST", "composite/sobjects", typed, "collections.create", sobject, all_or_none, concurrency
        )

    async def update(
        self,
        sobject: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """``PATCH composite/sobjects`` in chunks of 200. Each row needs ``Id``."""
        check_sobject(sobject)
        typed: list[dict[str, Any]] = []
        for r in rows:
            if not r.get("Id"):
                raise ValueError("each row needs an Id for update")
            typed.append(with_type(r, sobject))
        return await self._write(
            "PATCH", "composite/sobjects", typed, "collections.update", sobject, all_or_none, concurrency
        )

    async def upsert(
        self,
        sobject: str,
        field: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """``PATCH composite/sobjects/{o}/{field}`` in chunks of 200."""
        check_sobject(sobject)
        soql_identifier(field)
        typed = [with_type(r, sobject) for r in rows]
        return await self._write(
            "PATCH",
            f"composite/sobjects/{sobject}/{field}",
            typed,
            "collections.upsert",
            sobject,
            all_or_none,
            concurrency,
        )

    async def delete(
        self,
        ids: Iterable[str],
        *,
        all_or_none: bool = False,
        concurrency: int | None = None,
    ) -> BatchResult[RecordResult]:
        """``DELETE composite/sobjects?ids=`` in chunks of 200."""
        id_list = [check_record_id(i) for i in ids]
        if not id_list:
            return BatchResult([], operation="collections.delete")
        self._warn_atomic(all_or_none, len(id_list))

        async def one(chunk: Sequence[str]) -> list[RecordResult]:
            raw = await self._sf._send(
                RequestSpec(
                    "DELETE",
                    "composite/sobjects",
                    params={"ids": ",".join(chunk), "allOrNone": "true" if all_or_none else "false"},
                    operation="collections.delete",
                    records=len(chunk),
                    dml=True,
                )
            )
            results = parse_record_results(raw.json(), len(chunk))
            if all_or_none:
                self._raise_atomic(results, "collections.delete", None, "DELETE")
            return results

        return BatchResult(
            await self._run(
                one,
                list(chunked(id_list, COLLECTION_WRITE_CHUNK)),
                all_or_none,
                concurrency,
                operation="collections.delete",
                sobject=None,
            ),
            operation="collections.delete",
        )

    # --- read --------------------------------------------------------------------------

    async def retrieve(
        self,
        sobject: str,
        ids: Iterable[str],
        fields: Sequence[str],
        *,
        concurrency: int | None = None,
    ) -> list[dict[str, Any] | None]:
        """``POST composite/sobjects/{o}`` in chunks of 2,000. ``None`` for ids not found."""
        check_sobject(sobject)
        field_list = [soql_identifier(f) for f in fields]
        if not field_list:
            raise ValueError("fields must not be empty")
        id_list = [check_record_id(i) for i in ids]

        async def one(chunk: Sequence[str]) -> list[dict[str, Any] | None]:
            content, ctype = self._sf._transport.encode({"ids": list(chunk), "fields": field_list})
            raw = await self._sf._send(
                RequestSpec(
                    "POST",
                    f"composite/sobjects/{sobject}",
                    content=content,
                    content_type=ctype,
                    idempotent=True,
                    operation="collections.retrieve",
                    sobject=sobject,
                    compress=True,
                )
            )
            body: Any = raw.json()
            if not isinstance(body, list) or len(body) != len(chunk):  # pyright: ignore[reportUnknownArgumentType]
                raise UnexpectedResponse("unexpected sObject Collections retrieve response")
            return list(body)  # pyright: ignore[reportUnknownArgumentType]

        return await self._run(
            one,
            list(chunked(id_list, COLLECTION_READ_CHUNK)),
            False,
            concurrency,
            operation="collections.retrieve",
            sobject=sobject,
            writes=False,
        )

    # --- internals ---------------------------------------------------------------------

    def _warn_atomic(self, all_or_none: bool, n: int) -> None:
        if all_or_none and n > COLLECTION_WRITE_CHUNK:
            _log.warning(
                "esf: all_or_none=True with %d rows: each chunk of %d is atomic, but earlier chunks "
                "stay committed if a later chunk fails",
                n,
                COLLECTION_WRITE_CHUNK,
            )

    def _raise_atomic(
        self,
        results: list[RecordResult],
        operation: str,
        sobject: str | None,
        method: str = "POST",
        path: str = "composite/sobjects",
    ) -> None:
        failed = [r for r in results if not r.ok]
        if not failed:
            return
        details = [d for r in failed for d in r.errors]
        primary = next((d for d in details if d.error_code != _ROLLED_BACK), details[0] if details else None)
        if primary is None:
            raise UnexpectedResponse("all_or_none batch failed without error details", operation=operation)
        spec = RequestSpec(method, path, operation=operation, sobject=sobject, dml=True)
        raise error_from_detail(
            primary,
            status=200,
            spec=spec,
            dml=True,
            error_tags=self._sf._transport.error_tags,
            errors=tuple(details),
        )

    async def _run(
        self,
        fn: Any,
        chunks: list[Any],
        all_or_none: bool,
        concurrency: int | None,
        *,
        operation: str,
        sobject: str | None,
        writes: bool = True,
    ) -> list[Any]:
        if all_or_none:
            # One chunk at a time; the first failure raises. Earlier chunks stay committed.
            out: list[Any] = []
            for c in chunks:
                out.extend(await fn(c))
            return out
        n = concurrency or self.concurrency
        settled = await _compat.gather_settled(n, [functools.partial(fn, c) for c in chunks])
        results: list[Any] = []
        failures: list[SalesforceError] = []
        for chunk, part in zip(chunks, settled, strict=True):
            if isinstance(part, Exception):
                if not isinstance(part, SalesforceError):
                    raise part
                failures.append(part)
                results.extend(_chunk_failed(part) for _ in range(len(chunk)))
            else:
                results.extend(part)
        if failures and (not writes or len(failures) == len(chunks)):
            raise failures[0]  # nothing partial to report
        if failures:
            first = failures[0]
            code = first.error_code or type(first).__name__
            raise PartialFailure(
                f"{len(failures)} of {len(chunks)} chunks failed ({code}); "
                "the other chunks were sent and their results are in .results",
                error_code=first.error_code,
                status=first.status,
                operation=operation,
                sobject=sobject,
                errors=tuple(d for r in results if not r.ok for d in r.errors),
                results=BatchResult(results, operation=operation, sobject=sobject),
            ) from first
        return results

    async def _write(
        self,
        method: str,
        path: str,
        rows: list[dict[str, Any]],
        operation: str,
        sobject: str | None,
        all_or_none: bool,
        concurrency: int | None,
        *,
        idempotent: bool | None = None,
    ) -> BatchResult[RecordResult]:
        if not rows:
            return BatchResult([], operation=operation, sobject=sobject)
        self._warn_atomic(all_or_none, len(rows))

        async def one(chunk: Sequence[dict[str, Any]]) -> list[RecordResult]:
            content, ctype = self._sf._transport.encode({"allOrNone": all_or_none, "records": list(chunk)})
            raw = await self._sf._send(
                RequestSpec(
                    method,
                    path,
                    content=content,
                    content_type=ctype,
                    idempotent=idempotent,
                    operation=operation,
                    sobject=sobject,
                    records=len(chunk),
                    compress=True,
                    dml=True,
                )
            )
            results = parse_record_results(raw.json(), len(chunk))
            if all_or_none:
                self._raise_atomic(results, operation, sobject, method, path)
            return results

        results = await self._run(
            one,
            list(chunked(rows, COLLECTION_WRITE_CHUNK)),
            all_or_none,
            concurrency,
            operation=operation,
            sobject=sobject,
        )
        return BatchResult(results, operation=operation, sobject=sobject)
