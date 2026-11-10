"""Framework-neutral mapping of typed errors to HTTP responses (§13.1). No I/O, no framework."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..errors import (
    LimitError,
    MalformedQuery,
    NotFound,
    ReauthorizationRequired,
    RecordError,
    RequestLimitExceeded,
    SalesforceError,
    ServerError,
    TransportError,
)

__all__ = ["DEFAULT_RETRY_AFTER", "HANDLED", "HttpError", "http_error"]

DEFAULT_RETRY_AFTER = 30
"""``Retry-After`` seconds sent with a 503 when Salesforce gave no retry hint."""

# The classes the integrations register handlers for. Subclasses are handled through them.
HANDLED: tuple[type[SalesforceError], ...] = (
    ReauthorizationRequired,
    RecordError,
    NotFound,
    MalformedQuery,
    LimitError,
    TransportError,
    ServerError,
)


@dataclass(frozen=True)
class HttpError:
    """The response an integration sends for a mapped error."""

    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict[str, str])


def http_error(exc: BaseException) -> HttpError | None:
    """Map a typed error to a response, or ``None`` to let the framework treat it as a 500.

    Bodies carry a stable ``code`` for the app's own clients. ``RecordError`` bodies also
    carry the Salesforce ``message``, which can quote record values: only send it to callers
    allowed to see the record.
    """
    if isinstance(exc, ReauthorizationRequired):
        return HttpError(401, {"code": "salesforce_reauth_required"})
    if isinstance(exc, RecordError):
        return HttpError(
            422,
            {
                "code": "salesforce_record_error",
                "error_code": exc.error_code,
                "message": exc.message,
                "fields": list(exc.fields),
            },
        )
    if isinstance(exc, NotFound):
        return HttpError(404, {"code": "salesforce_not_found", "error_code": exc.error_code})
    if isinstance(exc, MalformedQuery):
        # Only a cursor the HTTP client sent back is the client's fault; bad SOQL is the app's bug.
        if exc.error_code == "INVALID_CURSOR":
            return HttpError(400, {"code": "salesforce_invalid_cursor", "error_code": exc.error_code})
        return None
    if isinstance(exc, LimitError):
        retry_after = DEFAULT_RETRY_AFTER
        if isinstance(exc, RequestLimitExceeded) and exc.retry_after is not None:
            retry_after = max(1, math.ceil(exc.retry_after))
        return HttpError(
            503,
            {"code": "salesforce_limit", "error_code": exc.error_code},
            {"Retry-After": str(retry_after)},
        )
    if isinstance(exc, TransportError):
        return HttpError(502, {"code": "salesforce_unavailable"})
    if isinstance(exc, ServerError):
        return HttpError(503, {"code": "salesforce_unavailable", "error_code": exc.error_code})
    return None
