"""Lead conversion: ``sf.leads`` (§9.8, D14). Uses the Partner SOAP ``convertLead`` call."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from ...core.records import chunked
from ...core.soap import CONVERT_CHUNK, convert_lead_envelope, lead_converts, parse_convert_results
from ...errors import ErrorDetail, PartialFailure, SalesforceError, ServerError
from ...models import BatchResult, LeadConvert, LeadConvertResult
from .collections import _chunk_failed  # pyright: ignore[reportPrivateUsage]
from .soap import result_error, soap_send

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["AsyncLeads"]

_OP = "leads.convert"


def _failed_rows(exc: SalesforceError) -> tuple[ErrorDetail, ...]:
    """Error entry for the rows of a call that failed as a whole.

    A parsed SOAP fault means Salesforce refused the call, so nothing converted
    (``"rejected"``), even though faults arrive as HTTP 500. Server-side faults and
    everything else follow the Collections rule.
    """
    fault = bool(exc.errors) and "fault_code" in exc.errors[0].extended
    server_side = isinstance(exc, ServerError) or exc.error_code == "UNKNOWN_EXCEPTION"
    if fault and not server_side:
        return (
            ErrorDetail(
                error_code=exc.error_code or type(exc).__name__,
                message=exc.message,
                extended={"chunk_error": type(exc).__name__, "outcome": "rejected"},
            ),
        )
    return _chunk_failed(exc).errors


class AsyncLeads:
    """Convert leads into accounts, contacts and opportunities.

    Salesforce has no REST resource for this, so the client sends a SOAP ``convertLead``
    call with your OAuth access token. You never see the SOAP part.
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client

    async def convert(
        self,
        lead_id: str,
        *,
        converted_status: str,
        account_id: str | None = None,
        contact_id: str | None = None,
        opportunity_name: str | None = None,
        do_not_create_opportunity: bool = False,
        owner_id: str | None = None,
        send_email: bool = False,
        overwrite_lead_source: bool = False,
    ) -> LeadConvertResult:
        """Convert one lead.

        Args:
            lead_id: The lead to convert.
            converted_status: A converted ``LeadStatus`` value of your org.
            account_id: Merge into this existing account instead of creating one.
            contact_id: Merge into this existing contact. Needs ``account_id``.
            opportunity_name: Name of the new opportunity.
            do_not_create_opportunity: Don't create an opportunity.
            owner_id: Owner of the new records. Defaults to the lead owner.
            send_email: Email the owner about the new records.
            overwrite_lead_source: Overwrite ``LeadSource`` on an existing contact.

        Returns:
            The ids of the converted lead and of the account, contact and opportunity.

        Raises:
            RecordError: Salesforce refused the conversion; the subclass follows the status code.
            ValueError: an id is malformed or ``converted_status`` is empty.
        """
        item = LeadConvert(
            lead_id,
            converted_status,
            account_id=account_id,
            contact_id=contact_id,
            opportunity_name=opportunity_name,
            do_not_create_opportunity=do_not_create_opportunity,
            owner_id=owner_id,
            send_email=send_email,
            overwrite_lead_source=overwrite_lead_source,
        )
        (result,) = await self._send([item])
        if not result.ok:
            raise result_error(self._sf, result.errors, _OP, "Lead")
        return result

    async def convert_many(self, converts: Iterable[LeadConvert]) -> BatchResult[LeadConvertResult]:
        """Convert many leads, 100 per SOAP call. Results keep the input order.

        Each lead converts on its own: one failure doesn't roll back the others. Check
        ``result.failed`` or call ``result.raise_for_errors()``.

        Raises:
            PartialFailure: a whole call of 100 failed while others succeeded. ``.results`` holds
                every row; rows of the failed call have ``extended["outcome"]`` set to
                ``"unknown"`` (they may have converted) or ``"rejected"``.
            SalesforceError: every call failed.
        """
        items = lead_converts(converts)
        if not items:
            return BatchResult([], operation=_OP, sobject="Lead")
        chunks = list(chunked(items, CONVERT_CHUNK))
        results: list[LeadConvertResult] = []
        failures: list[SalesforceError] = []
        for chunk in chunks:
            try:
                results.extend(await self._send(chunk))
            except SalesforceError as exc:
                failures.append(exc)
                detail = _failed_rows(exc)
                results.extend(LeadConvertResult(ok=False, lead_id=c.lead_id, errors=detail) for c in chunk)
        if failures and len(failures) == len(chunks):
            raise failures[0]
        if failures:
            first = failures[0]
            raise PartialFailure(
                f"{len(failures)} of {len(chunks)} convertLead calls failed "
                f"({first.error_code or type(first).__name__}); the others were sent",
                error_code=first.error_code,
                status=first.status,
                operation=_OP,
                sobject="Lead",
                errors=tuple(d for r in results if not r.ok for d in r.errors),
                results=BatchResult(results, operation=_OP, sobject="Lead"),
            ) from first
        return BatchResult(results, operation=_OP, sobject="Lead")

    async def _send(self, chunk: Sequence[LeadConvert]) -> list[LeadConvertResult]:
        items = list(chunk)
        raw = await soap_send(
            self._sf,
            lambda session: convert_lead_envelope(items, session),
            operation=_OP,
            sobject="Lead",
            records=len(items),
        )
        return parse_convert_results(raw.content, len(items))
