"""Records: ``sf.sobject(name)`` (§9.1)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any, TypeVar, overload

from ...core.http import RequestSpec, check_record_id, check_sobject, url_segment
from ...core.records import apply_model, duplicate_rule_header, format_sf_datetime, parse_upsert
from ...core.soql import soql_identifier
from ...errors import UnexpectedResponse
from ...models import UpsertResult

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["AsyncSObject"]

M = TypeVar("M")


def _http_date(dt: datetime) -> str:
    """RFC 7231 date for conditional headers."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime is ambiguous; attach a timezone (e.g. UTC)")
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def _write_headers(
    assignment_rule_id: str | bool | None,
    allow_save: bool | None,
    if_unmodified_since: datetime | None = None,
) -> dict[str, str]:
    h: dict[str, str] = {}
    if assignment_rule_id is not None:
        if isinstance(assignment_rule_id, bool):
            h["Sforce-Auto-Assign"] = "TRUE" if assignment_rule_id else "FALSE"
        else:
            h["Sforce-Auto-Assign"] = check_record_id(assignment_rule_id)
    if allow_save is not None:
        h["Sforce-Duplicate-Rule-Header"] = duplicate_rule_header(allow_save=allow_save)
    if if_unmodified_since is not None:
        h["If-Unmodified-Since"] = _http_date(if_unmodified_since)
    return h


class AsyncSObject:
    """Operations on one sObject type. Get one with ``sf.sobject("Lead")``."""

    def __init__(self, client: AsyncSalesforce, name: str) -> None:
        self._sf = client
        self.name = check_sobject(name)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"

    # --- read --------------------------------------------------------------------------

    @overload
    async def get(
        self,
        record_id: str,
        fields: Sequence[str] | None = None,
        *,
        model: None = None,
        if_modified_since: None = None,
    ) -> dict[str, Any]: ...
    @overload
    async def get(
        self,
        record_id: str,
        fields: Sequence[str] | None = None,
        *,
        model: None = None,
        if_modified_since: datetime,
    ) -> dict[str, Any] | None: ...
    @overload
    async def get(
        self,
        record_id: str,
        fields: Sequence[str] | None = None,
        *,
        model: type[M],
        if_modified_since: None = None,
    ) -> M: ...
    @overload
    async def get(
        self,
        record_id: str,
        fields: Sequence[str] | None = None,
        *,
        model: type[M],
        if_modified_since: datetime,
    ) -> M | None: ...
    async def get(
        self,
        record_id: str,
        fields: Sequence[str] | None = None,
        *,
        model: Any = None,
        if_modified_since: datetime | None = None,
    ) -> Any:
        """``GET sobjects/{o}/{id}``. With ``if_modified_since``, returns ``None`` on 304."""
        params = {"fields": ",".join(soql_identifier(f) for f in fields)} if fields else None
        headers = {"If-Modified-Since": _http_date(if_modified_since)} if if_modified_since else {}
        raw = await self._sf._send(
            RequestSpec(
                "GET",
                f"sobjects/{self.name}/{check_record_id(record_id)}",
                params=params,
                headers=headers,
                operation="sobject.get",
                sobject=self.name,
                allow_not_modified=bool(if_modified_since),
            )
        )
        if raw.status == 304:
            return None
        return apply_model([raw.json()], model)[0]

    async def get_by_external_id(
        self, field: str, value: str, fields: Sequence[str] | None = None, *, model: Any = None
    ) -> Any:
        """``GET sobjects/{o}/{field}/{value}``."""
        params = {"fields": ",".join(soql_identifier(f) for f in fields)} if fields else None
        raw = await self._sf._send(
            RequestSpec(
                "GET",
                f"sobjects/{self.name}/{soql_identifier(field)}/{url_segment(str(value))}",
                params=params,
                operation="sobject.get_by_external_id",
                sobject=self.name,
                path_template=f"/sobjects/{self.name}/{field}/:value",
            )
        )
        return apply_model([raw.json()], model)[0]

    async def describe(self, *, cached: bool = True) -> dict[str, Any]:
        """``GET sobjects/{o}/describe``, served from the describe cache when fresh."""
        return await self._sf._describe(
            f"sobjects/{self.name}/describe", f"sobject:{self.name}", cached, self.name
        )

    async def metadata(self) -> dict[str, Any]:
        """``GET sobjects/{o}`` (basic info and recent items)."""
        raw = await self._sf._send(
            RequestSpec("GET", f"sobjects/{self.name}", operation="sobject.metadata", sobject=self.name)
        )
        return raw.json()

    async def updated(self, start: datetime, end: datetime) -> dict[str, Any]:
        """``GET sobjects/{o}/updated?start=&end=``: ids updated in the window."""
        return await self._changed("updated", start, end)

    async def deleted(self, start: datetime, end: datetime) -> dict[str, Any]:
        """``GET sobjects/{o}/deleted?start=&end=``: ids deleted in the window."""
        return await self._changed("deleted", start, end)

    async def _changed(self, kind: str, start: datetime, end: datetime) -> dict[str, Any]:
        raw = await self._sf._send(
            RequestSpec(
                "GET",
                f"sobjects/{self.name}/{kind}/",
                params={"start": format_sf_datetime(start), "end": format_sf_datetime(end)},
                operation=f"sobject.{kind}",
                sobject=self.name,
            )
        )
        return raw.json()

    # --- write -------------------------------------------------------------------------

    async def create(
        self,
        data: Mapping[str, Any],
        *,
        assignment_rule_id: str | bool | None = None,
        allow_save: bool | None = None,
    ) -> str:
        """``POST sobjects/{o}``. Returns the new record id.

        Args:
            data: Field values.
            assignment_rule_id: ``Sforce-Auto-Assign``: an AssignmentRule id, or ``True``/``False``.
            allow_save: Send ``Sforce-Duplicate-Rule-Header: allowSave=...``.
        """
        content, ctype = self._sf._transport.encode(dict(data))
        raw = await self._sf._send(
            RequestSpec(
                "POST",
                f"sobjects/{self.name}/",
                content=content,
                content_type=ctype,
                headers=_write_headers(assignment_rule_id, allow_save),
                operation="sobject.create",
                sobject=self.name,
                records=1,
                dml=True,
            )
        )
        body = raw.json()
        if not isinstance(body, dict) or not body.get("id"):  # pyright: ignore[reportUnknownMemberType]
            raise UnexpectedResponse(
                "create returned no id", raw=raw.content[:4096], operation="sobject.create"
            )
        return str(body["id"])  # pyright: ignore[reportUnknownArgumentType]

    async def update(
        self,
        record_id: str,
        data: Mapping[str, Any],
        *,
        assignment_rule_id: str | bool | None = None,
        allow_save: bool | None = None,
        if_unmodified_since: datetime | None = None,
    ) -> None:
        """``PATCH sobjects/{o}/{id}``. ``if_unmodified_since`` → :class:`Conflict` on 412."""
        content, ctype = self._sf._transport.encode(dict(data))
        await self._sf._send(
            RequestSpec(
                "PATCH",
                f"sobjects/{self.name}/{check_record_id(record_id)}",
                content=content,
                content_type=ctype,
                headers=_write_headers(assignment_rule_id, allow_save, if_unmodified_since),
                operation="sobject.update",
                sobject=self.name,
                records=1,
                dml=True,
            )
        )

    async def upsert(
        self,
        field: str,
        value: str,
        data: Mapping[str, Any],
        *,
        assignment_rule_id: str | bool | None = None,
        allow_save: bool | None = None,
    ) -> UpsertResult:
        """``PATCH sobjects/{o}/{field}/{value}``. Raises :class:`MultipleMatches` on HTTP 300."""
        content, ctype = self._sf._transport.encode(dict(data))
        raw = await self._sf._send(
            RequestSpec(
                "PATCH",
                f"sobjects/{self.name}/{soql_identifier(field)}/{url_segment(str(value))}",
                content=content,
                content_type=ctype,
                headers=_write_headers(assignment_rule_id, allow_save),
                operation="sobject.upsert",
                sobject=self.name,
                path_template=f"/sobjects/{self.name}/{field}/:value",
                records=1,
                dml=True,
            )
        )
        body = raw.json() if raw.content else None
        return parse_upsert(raw.status, body)

    async def delete(self, record_id: str) -> None:
        """``DELETE sobjects/{o}/{id}``.

        Takes no conditional headers: Salesforce doesn't support them on DELETE
        (REST API Developer Guide, *Conditional Request Headers*), so a precondition would be
        ignored and the record deleted anyway.
        """
        await self._sf._send(
            RequestSpec(
                "DELETE",
                f"sobjects/{self.name}/{check_record_id(record_id)}",
                operation="sobject.delete",
                sobject=self.name,
                records=1,
                dml=True,
            )
        )
