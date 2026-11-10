"""Partner SOAP calls behind normal methods (§9.8, D14): one send path for convert and merge."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from ...core.classify import error_from_detail
from ...core.http import RawResponse, RequestSpec, check_record_id, check_sobject
from ...core.soap import (
    MERGE_MAX_DUPLICATES,
    MERGE_SOBJECTS,
    merge_envelope,
    parse_merge_results,
    soap_path,
)
from ...errors import ErrorDetail, SalesforceError, UnexpectedResponse
from ...models import MergeResult

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["merge", "result_error", "soap_send"]


async def soap_send(
    sf: AsyncSalesforce,
    render: Callable[[str], bytes],
    *,
    operation: str,
    sobject: str | None,
    records: int,
) -> RawResponse:
    """POST a Partner SOAP envelope through the pipeline; ``render`` gets the access token.

    Never resent after it may have reached Salesforce (it writes records). A rejected session
    (``INVALID_SESSION_ID`` fault) refreshes the token once and resends with a new envelope.
    """
    return await sf._send(  # pyright: ignore[reportPrivateUsage]
        RequestSpec(
            "POST",
            soap_path(sf.api_version),
            headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": '""', "Accept": "text/xml"},
            content_type="text/xml; charset=UTF-8",
            session_body=render,
            operation=operation,
            sobject=sobject,
            records=records,
            dml=True,
            expect_json=False,
        )
    )


def result_error(
    sf: AsyncSalesforce, errors: Sequence[ErrorDetail], operation: str, sobject: str | None
) -> SalesforceError:
    """The typed error for a failed SOAP result (``success=false`` with ``errors``)."""
    if not errors:
        return UnexpectedResponse(f"{operation} failed without error details", operation=operation)
    spec = RequestSpec("POST", soap_path(sf.api_version), operation=operation, sobject=sobject, dml=True)
    return error_from_detail(
        errors[0],
        status=200,
        spec=spec,
        dml=True,
        error_tags=sf._transport.error_tags,  # pyright: ignore[reportPrivateUsage]
        errors=tuple(errors),
    )


async def merge(
    sf: AsyncSalesforce, master_id: str, duplicate_ids: Sequence[str], sobject: str
) -> MergeResult:
    """See :meth:`AsyncSalesforce.merge`."""
    check_sobject(sobject)
    if sobject not in MERGE_SOBJECTS:
        raise ValueError(f"merge supports {', '.join(sorted(MERGE_SOBJECTS))}, not {sobject!r}")
    check_record_id(master_id)
    if isinstance(duplicate_ids, str):
        raise TypeError("duplicate_ids must be a list of ids, not one string")
    dups = [check_record_id(i) for i in duplicate_ids]
    if not 1 <= len(dups) <= MERGE_MAX_DUPLICATES:
        raise ValueError(f"merge takes 1 to {MERGE_MAX_DUPLICATES} duplicate ids per call, got {len(dups)}")
    keys = [i[:15] for i in dups]
    if master_id[:15] in keys or len(set(keys)) != len(keys):
        raise ValueError("duplicate_ids must be distinct and must not contain master_id")

    raw = await soap_send(
        sf,
        lambda session: merge_envelope(sobject, master_id, dups, session),
        operation="merge",
        sobject=sobject,
        records=1 + len(dups),
    )
    (result,) = parse_merge_results(raw.content, 1)
    if not result.ok:
        raise result_error(sf, result.errors, "merge", sobject)
    return result
