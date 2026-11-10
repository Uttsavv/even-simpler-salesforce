"""Fakes of ``sf.leads``, ``sf.merge`` and ``sf.platform_events`` (§14).

Lead conversion, merge and event publishing act on the fake's in-memory records, so a test
can read the new account, contact and opportunity, or the events it published.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..core.events import check_event_name, event_error
from ..core.http import check_record_id, check_sobject
from ..core.records import with_type
from ..core.soap import MERGE_MAX_DUPLICATES, MERGE_SOBJECTS, lead_converts, soap_path
from ..errors import ErrorDetail, RecordError, SalesforceError
from ..models import BatchResult, LeadConvert, LeadConvertResult, MergeResult, PublishResult
from ._state import FakeState, Who

__all__ = [
    "AsyncFakeLeads",
    "AsyncFakePlatformEvents",
    "FakeLeads",
    "FakePlatformEvents",
    "fake_merge",
]

_CONVERT = "leads.convert"


def _detail(exc: SalesforceError) -> ErrorDetail:
    return exc.errors[0] if exc.errors else ErrorDetail(exc.error_code, exc.message, exc.fields)


def _convert_one(state: FakeState, c: LeadConvert) -> LeadConvertResult:
    lead = state.find("Lead", c.lead_id, _CONVERT)
    if lead.get("IsConverted"):
        raise RecordError(
            "This lead is already converted",
            error_code="CANNOT_UPDATE_CONVERTED_LEAD",
            operation=_CONVERT,
            sobject="Lead",
        )
    state._check_rejections("Lead", lead)  # pyright: ignore[reportPrivateUsage]
    company = lead.get("Company") or lead.get("LastName") or "Converted lead"
    if c.account_id is not None:
        account_id = str(state.find("Account", c.account_id, _CONVERT)["Id"])
    else:
        account_id = state.create("Account", {"Name": company}, _CONVERT)
    if c.contact_id is not None:
        contact_id = str(state.find("Contact", c.contact_id, _CONVERT)["Id"])
    else:
        contact = {k: lead[k] for k in ("FirstName", "LastName", "Email", "Phone") if lead.get(k) is not None}
        contact["AccountId"] = account_id
        contact_id = state.create("Contact", contact, _CONVERT)
    opportunity_id: str | None = None
    if not c.do_not_create_opportunity:
        opportunity_id = state.create(
            "Opportunity", {"Name": c.opportunity_name or company, "AccountId": account_id}, _CONVERT
        )
    update: dict[str, Any] = {
        "Status": c.converted_status,
        "IsConverted": True,
        "ConvertedAccountId": account_id,
        "ConvertedContactId": contact_id,
        "ConvertedOpportunityId": opportunity_id,
    }
    if c.owner_id is not None:
        update["OwnerId"] = c.owner_id
    lead.update(update)
    return LeadConvertResult(
        ok=True,
        lead_id=str(lead["Id"]),
        account_id=account_id,
        contact_id=contact_id,
        opportunity_id=opportunity_id,
    )


def _convert_many(state: FakeState, items: Sequence[LeadConvert]) -> list[LeadConvertResult]:
    out: list[LeadConvertResult] = []
    for c in items:
        snap = state._snap()  # pyright: ignore[reportPrivateUsage]
        try:
            out.append(_convert_one(state, c))
        except SalesforceError as exc:
            state._restore(snap)  # pyright: ignore[reportPrivateUsage]
            out.append(LeadConvertResult(ok=False, lead_id=c.lead_id, errors=(_detail(exc),)))
    return out


class FakeLeads:
    """Fake of ``sf.leads``: converts stored leads into stored accounts, contacts and opportunities.

    The lead gets ``IsConverted=True``, the new ``Status`` and the ``Converted*Id`` fields. A
    lead that is already converted fails with ``CANNOT_UPDATE_CONVERTED_LEAD``, and
    ``fake.reject("Lead", error)`` makes conversions fail per lead.
    """

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    def convert(
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
        """Convert one stored lead; raises the error a failed conversion would raise."""
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

        def fn() -> LeadConvertResult:
            snap = self._state._snap()  # pyright: ignore[reportPrivateUsage]
            try:
                return _convert_one(self._state, item)
            except SalesforceError:
                self._state._restore(snap)  # pyright: ignore[reportPrivateUsage]
                raise

        return self._state.run(
            self._who,
            _CONVERT,
            "Lead",
            {"converts": [item]},
            fn,
            method="POST",
            path=soap_path(self._state.api_version),
            records=1,
        )

    def convert_many(self, converts: Iterable[LeadConvert]) -> BatchResult[LeadConvertResult]:
        """Convert many stored leads; per-lead failures are in the results."""
        items = lead_converts(converts)
        results: list[LeadConvertResult] = self._state.run(
            self._who,
            _CONVERT,
            "Lead",
            {"converts": items},
            lambda: _convert_many(self._state, items),
            method="POST",
            path=soap_path(self._state.api_version),
            records=len(items),
        )
        return BatchResult(results, operation=_CONVERT, sobject="Lead")


class AsyncFakeLeads:
    """Async twin of :class:`FakeLeads`."""

    def __init__(self, inner: FakeLeads) -> None:
        self._inner = inner

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
        """See :meth:`FakeLeads.convert`."""
        return self._inner.convert(
            lead_id,
            converted_status=converted_status,
            account_id=account_id,
            contact_id=contact_id,
            opportunity_name=opportunity_name,
            do_not_create_opportunity=do_not_create_opportunity,
            owner_id=owner_id,
            send_email=send_email,
            overwrite_lead_source=overwrite_lead_source,
        )

    async def convert_many(self, converts: Iterable[LeadConvert]) -> BatchResult[LeadConvertResult]:
        """See :meth:`FakeLeads.convert_many`."""
        return self._inner.convert_many(converts)


def fake_merge(
    state: FakeState, who: Who, master_id: str, duplicate_ids: Sequence[str], sobject: str
) -> MergeResult:
    """``sf.merge`` on the fake: deletes the duplicates and repoints fields that held their ids."""
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

    def fn() -> MergeResult:
        master = state.find(sobject, master_id, "merge")
        victims = [state.find(sobject, i, "merge") for i in dups]
        victim_keys = {str(v["Id"])[:15] for v in victims}
        target = str(master["Id"])
        related: list[str] = []
        for name, recs in state.store.items():
            for key, rec in recs.items():
                if name == sobject and key in victim_keys:
                    continue
                hit = False
                for field, value in rec.items():
                    is_id = isinstance(value, str) and len(value) in (15, 18)
                    if field != "Id" and is_id and value[:15] in victim_keys:
                        rec[field] = target
                        hit = True
                if hit:
                    related.append(str(rec["Id"]))
        for v in victims:
            state.delete(sobject, str(v["Id"]), operation="merge")
        return MergeResult(
            ok=True,
            id=target,
            merged_ids=tuple(str(v["Id"]) for v in victims),
            updated_related_ids=tuple(related),
        )

    return state.run(
        who,
        "merge",
        sobject,
        {"master_id": master_id, "duplicate_ids": dups},
        fn,
        method="POST",
        path=soap_path(state.api_version),
        records=1 + len(dups),
    )


class FakePlatformEvents:
    """Fake of ``sf.platform_events``: keeps published events for ``fake.published(name)``.

    ``fake.reject("Order_Event__e", error)`` makes publishes of that event fail, and
    ``fake.fail_next("platform_events.publish", error)`` fails the next single publish.
    """

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    def _publish(self, name: str, data: Mapping[str, Any]) -> PublishResult:
        self._state._check_rejections(name, data)  # pyright: ignore[reportPrivateUsage]
        event = {k: copy.deepcopy(v) for k, v in data.items() if k != "attributes"}
        self._state.events.setdefault(name, []).append(event)
        return PublishResult(ok=True, id=self._state.new_id(name), uuid=str(uuid.uuid4()))

    def publish(self, name: str, data: Mapping[str, Any]) -> PublishResult:
        """Store one event and return a queued :class:`PublishResult`."""
        check_event_name(name)

        def fn() -> PublishResult:
            try:
                return self._publish(name, data)
            except SalesforceError as exc:
                raise event_error(exc) from None

        return self._state.run(
            self._who,
            "platform_events.publish",
            name,
            {"data": dict(data)},
            fn,
            method="POST",
            path=f"sobjects/{name}",
            records=1,
        )

    def publish_many(self, name: str, rows: Iterable[Mapping[str, Any]]) -> BatchResult[PublishResult]:
        """Store many events (a ``collections.create`` call); per-event failures are in the results."""
        check_event_name(name)
        row_list = [with_type(r, name) for r in rows]

        def fn() -> BatchResult[PublishResult]:
            out: list[PublishResult] = []
            for row in row_list:
                try:
                    out.append(self._publish(name, row))
                except SalesforceError as exc:
                    out.append(PublishResult(ok=False, errors=(_detail(event_error(exc)),)))
            return BatchResult(out, operation="collections.create", sobject=name)

        return self._state.run(
            self._who,
            "collections.create",
            name,
            {"rows": row_list, "sobject": name, "all_or_none": False},
            fn,
            method="POST",
            path="composite/sobjects",
            records=len(row_list),
        )


class AsyncFakePlatformEvents:
    """Async twin of :class:`FakePlatformEvents`."""

    def __init__(self, inner: FakePlatformEvents) -> None:
        self._inner = inner

    async def publish(self, name: str, data: Mapping[str, Any]) -> PublishResult:
        """See :meth:`FakePlatformEvents.publish`."""
        return self._inner.publish(name, data)

    async def publish_many(self, name: str, rows: Iterable[Mapping[str, Any]]) -> BatchResult[PublishResult]:
        """See :meth:`FakePlatformEvents.publish_many`."""
        return self._inner.publish_many(name, rows)
