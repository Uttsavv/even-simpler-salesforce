"""Turn Salesforce responses into typed errors (§8). No I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .. import errors as E
from .http import RawResponse, RequestSpec, path_template

__all__ = [
    "CODE_MAP",
    "TAG_PATTERN",
    "classify",
    "error_class_for",
    "error_from_detail",
    "parse_details",
    "parse_retry_after",
]

TAG_PATTERN = re.compile(r"^\[(?P<tag>[A-Z0-9_]{2,64})\]\s*")
_RAW_LIMIT = 4096

CODE_MAP: dict[str, type[E.SalesforceError]] = {
    # request
    "MALFORMED_QUERY": E.MalformedQuery,
    "INVALID_QUERY_LOCATOR": E.MalformedQuery,
    "INVALID_QUERY_FILTER_OPERATOR": E.MalformedQuery,
    "MALFORMED_SEARCH": E.MalformedQuery,
    "INVALID_SEARCH": E.MalformedQuery,
    "INVALID_FIELD": E.InvalidField,
    "INVALID_FIELD_FOR_INSERT_UPDATE": E.InvalidField,
    "INVALID_FIELD_WHEN_USING_TEMPLATE": E.InvalidField,
    "INVALID_TYPE": E.InvalidType,
    "NOT_FOUND": E.NotFound,
    "ENTITY_IS_DELETED": E.NotFound,
    "INSUFFICIENT_ACCESS": E.Forbidden,
    "INSUFFICIENT_ACCESS_OR_READONLY": E.Forbidden,
    "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY": E.Forbidden,
    "API_DISABLED_FOR_ORG": E.Forbidden,
    "API_CURRENTLY_DISABLED": E.Forbidden,
    "FUNCTIONALITY_NOT_ENABLED": E.Forbidden,
    # record
    "FIELD_CUSTOM_VALIDATION_EXCEPTION": E.ValidationRuleFailed,
    "REQUIRED_FIELD_MISSING": E.RequiredFieldMissing,
    "DUPLICATES_DETECTED": E.DuplicateDetected,
    "INVALID_CROSS_REFERENCE_KEY": E.InvalidReference,
    "FIELD_INTEGRITY_EXCEPTION": E.InvalidReference,
    "STRING_TOO_LONG": E.StringTooLong,
    "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST": E.InvalidPicklistValue,
    "UNABLE_TO_LOCK_ROW": E.RowLocked,
    "CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY": E.RecordSaveBlocked,
    "CANNOT_EXECUTE_FLOW_TRIGGER": E.FlowFailed,
    "STORAGE_LIMIT_EXCEEDED": E.StorageLimitExceeded,
    # limits / auth / server
    "REQUEST_LIMIT_EXCEEDED": E.RequestLimitExceeded,
    "INVALID_SESSION_ID": E.SessionExpired,
    "SERVER_UNAVAILABLE": E.ServerError,
    "APEX_ERROR": E.ApexError,
    "UNSUPPORTED_API_VERSION": E.UnsupportedApiVersion,
}

_STATUS_MAP: dict[int, type[E.SalesforceError]] = {
    300: E.MultipleMatches,
    401: E.SessionExpired,
    403: E.Forbidden,
    404: E.NotFound,
    409: E.Conflict,
    410: E.Gone,
    412: E.Conflict,
    414: E.RequestTooLarge,
    420: E.ServerError,
    428: E.Conflict,
    429: E.RequestLimitExceeded,
    431: E.RequestTooLarge,
}

_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
# Operations on an sObject *type* (no record id): NOT_FOUND there means the type doesn't exist.
_TYPE_OPS = frozenset({"sobject.describe", "sobject.metadata", "sobject.updated", "sobject.deleted"})


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return ()


def _detail_from_mapping(item: Mapping[str, Any]) -> E.ErrorDetail:
    code = item.get("errorCode") or item.get("statusCode")
    known = {"errorCode", "statusCode", "message", "fields"}
    return E.ErrorDetail(
        error_code=str(code) if code is not None else None,
        message=str(item.get("message") or ""),
        fields=_as_str_tuple(item.get("fields")),
        extended={k: v for k, v in item.items() if k not in known},
    )


def parse_details(body: Any) -> tuple[E.ErrorDetail, ...]:
    """Parse REST-style error entries (a list of ``{message, errorCode|statusCode, fields}``)."""
    if isinstance(body, Mapping):
        body = [body]
    if not isinstance(body, list):
        return ()
    out: list[E.ErrorDetail] = []
    for item in body:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(item, Mapping) and ("errorCode" in item or "statusCode" in item or "message" in item):
            out.append(_detail_from_mapping(item))  # pyright: ignore[reportUnknownArgumentType]
    return tuple(out)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


def error_class_for(code: str | None, status: int | None, *, dml: bool) -> type[E.SalesforceError]:
    """Pick the exception class for an error code / HTTP status."""
    if code and code in CODE_MAP:
        return CODE_MAP[code]
    if status is not None and status in _STATUS_MAP:
        return _STATUS_MAP[status]
    if status is not None and status >= 500:
        return E.ServerError
    return E.RecordError if dml else E.RequestError


def _apply_tag(
    cls: type[E.SalesforceError],
    message: str,
    error_tags: Mapping[str, type[E.SalesforceError]] | None,
) -> tuple[type[E.SalesforceError], str, str | None]:
    m = TAG_PATTERN.match(message)
    if not m:
        return cls, message, None
    tag = m.group("tag")
    registered = (error_tags or {}).get(tag)
    if registered is not None and issubclass(registered, cls):
        return registered, message[m.end() :], tag
    return cls, message, tag


def error_from_detail(
    detail: E.ErrorDetail,
    *,
    status: int | None = None,
    spec: RequestSpec | None = None,
    dml: bool = True,
    error_tags: Mapping[str, type[E.SalesforceError]] | None = None,
    errors: tuple[E.ErrorDetail, ...] = (),
    request_id: str | None = None,
    raw: bytes | str | None = None,
    body: Any = None,
) -> E.SalesforceError:
    """Build a typed exception for one error entry (``body`` is kept on :class:`ApexError`)."""
    cls = error_class_for(detail.error_code, status, dml=dml)
    if (
        cls is E.NotFound
        and detail.error_code == "NOT_FOUND"
        and spec is not None
        and spec.operation in _TYPE_OPS
    ):
        cls = E.InvalidType  # the sObject type itself doesn't exist (§8.2)
    cls, message, tag = _apply_tag(cls, detail.message, error_tags)
    kw: dict[str, Any] = {}
    if issubclass(cls, E.DuplicateDetected):
        kw["duplicates"] = detail.extended.get("duplicateResult")
    if issubclass(cls, E.ApexError):
        kw["body"] = body
    retryable: bool | None = None
    if cls is E.ServerError and status is not None:
        retryable = status in _RETRYABLE_STATUS or detail.error_code == "SERVER_UNAVAILABLE"
    return cls(
        message,
        error_code=detail.error_code,
        fields=detail.fields,
        status=status,
        operation=spec.operation if spec else None,
        sobject=spec.sobject if spec else None,
        method=spec.method if spec else None,
        path=(spec.path_template or path_template(spec.path)) if spec else None,
        request_id=request_id,
        retryable=retryable,
        errors=errors or (detail,),
        raw=raw,
        tag=tag,
        **kw,
    )


def _is_oauth_path(path: str) -> bool:
    return path.split("?", 1)[0].startswith("/services/oauth2/")


def _truncate(content: bytes) -> bytes:
    return content if len(content) <= _RAW_LIMIT else content[:_RAW_LIMIT]


def classify(
    resp: RawResponse,
    spec: RequestSpec,
    error_tags: Mapping[str, type[E.SalesforceError]] | None = None,
) -> E.SalesforceError | None:
    """Return ``None`` for success, else the typed error for this response."""
    status = resp.status
    if 200 <= status < 300 or (status == 304 and spec.allow_not_modified):
        return None

    common: dict[str, Any] = {
        "status": status,
        "operation": spec.operation,
        "sobject": spec.sobject,
        "method": spec.method,
        "path": spec.path_template or path_template(spec.path),
        "request_id": resp.request_id,
    }
    raw = _truncate(resp.content)

    body: Any = None
    parsed = False
    if resp.content:
        try:
            body = resp.json()
            parsed = True
        except ValueError:
            parsed = False

    if not parsed and resp.content:
        return E.UnexpectedResponse(
            f"non-JSON response (HTTP {status}, {resp.content_type or 'no content-type'})",
            raw=raw,
            retryable=status in _RETRYABLE_STATUS,
            **common,
        )

    # 300 Multiple Choices: external id matched several records; body is a list of URLs.
    if status == 300:
        matches: tuple[str, ...] = ()
        if isinstance(body, list):
            matches = tuple(str(x) for x in body)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        return E.MultipleMatches(
            f"external id matched {len(matches) or 'several'} records",
            matches=matches,
            raw=raw,
            **common,
        )

    # OAuth endpoints: {"error": "...", "error_description": "..."}. Only under /services/oauth2/:
    # an Apex REST class may return {"error": "..."} too, and that is not an OAuth failure.
    if (
        _is_oauth_path(spec.path)
        and isinstance(body, Mapping)
        and "error" in body
        and "errorCode" not in body
    ):
        err = str(body.get("error") or "")  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        desc = str(body.get("error_description") or "")  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        if err in ("invalid_token",) or status == 401:
            return E.SessionExpired(desc or err, error_code=err, raw=raw, **common)
        if err == "invalid_grant":
            return E.ReauthorizationRequired(desc or err, error_code=err, raw=raw, **common)
        return E.OAuthError(desc or err, error=err, error_description=desc, raw=raw, **common)

    details = parse_details(body)
    if details:
        first = details[0]
        exc = error_from_detail(
            first,
            status=status,
            spec=spec,
            dml=spec.dml,
            error_tags=error_tags,
            errors=details,
            request_id=resp.request_id,
            raw=raw,
            body=body,
        )
        if isinstance(exc, E.RequestLimitExceeded):
            exc.retry_after = parse_retry_after(resp.headers.get("retry-after"))
        return exc

    cls = error_class_for(None, status, dml=spec.dml)
    kw: dict[str, Any] = {}
    if cls is E.RequestLimitExceeded:
        kw["retry_after"] = parse_retry_after(resp.headers.get("retry-after"))
    retryable = status in _RETRYABLE_STATUS if cls is E.ServerError else None
    return cls(f"HTTP {status}", raw=raw or None, retryable=retryable, **common, **kw)
