"""Exception hierarchy.

Every error raised by the library is a :class:`SalesforceError`. All errors share one shape,
regardless of which Salesforce API produced them (REST, OAuth, Collections, SOAP, ...).

This module performs no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import BatchResult, UsageSnapshot

__all__ = [
    "ApexError",
    "AuthError",
    "BudgetExceeded",
    "BulkJobFailed",
    "Conflict",
    "ConnectError",
    "DuplicateDetected",
    "ErrorDetail",
    "EventPublishFailed",
    "EventPublishUnavailable",
    "FlowFailed",
    "Forbidden",
    "Gone",
    "GraphQLError",
    "InvalidField",
    "InvalidPicklistValue",
    "InvalidReference",
    "InvalidType",
    "LimitError",
    "MalformedQuery",
    "MultipleMatches",
    "NotFound",
    "OAuthError",
    "PartialFailure",
    "ReauthorizationRequired",
    "RecordError",
    "RecordSaveBlocked",
    "RefreshFailed",
    "RequestError",
    "RequestLimitExceeded",
    "RequestTimeout",
    "RequestTooLarge",
    "RequiredFieldMissing",
    "RowLocked",
    "SalesforceError",
    "ServerError",
    "SessionExpired",
    "SoapFault",
    "StorageLimitExceeded",
    "StringTooLong",
    "TokenConflict",
    "TransportError",
    "UnexpectedResponse",
    "UnsupportedApiVersion",
    "ValidationRuleFailed",
]


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """One entry of a (possibly multi-error) Salesforce error body."""

    error_code: str | None
    message: str
    fields: tuple[str, ...] = ()
    extended: Mapping[str, Any] = field(default_factory=lambda: {})


#: Troubleshooting page; each public error class has an entry whose anchor is its lower-cased name.
ERRORS_PAGE = "https://uttsavv.github.io/even-simpler-salesforce/latest/errors/"


def docs_url_for(cls: type[BaseException]) -> str | None:
    """The troubleshooting entry for ``cls``, e.g. ``.../errors/#invalidfield``.

    A user subclass (such as a class registered in ``error_tags``) gets the entry of its
    nearest library class.
    """
    for klass in cls.__mro__:
        if klass.__module__ == __name__ and klass.__name__ in __all__:
            return f"{ERRORS_PAGE}#{klass.__name__.lower()}"
    return None


def _rebuild(cls: type[SalesforceError], state: dict[str, Any]) -> SalesforceError:
    exc = cls.__new__(cls)
    Exception.__init__(exc, state.get("message", ""))
    exc.__dict__.update(state)
    return exc


class SalesforceError(Exception):
    """Base class for every error raised by the library.

    Attributes:
        message: Human-readable message, from Salesforce when available.
        error_code: Salesforce ``errorCode`` / OAuth ``error`` (e.g. ``"INVALID_FIELD"``).
        fields: Fields Salesforce reported as affected.
        status: HTTP status, if there was a response.
        operation: Library operation name, e.g. ``"sobject.update"``.
        sobject: sObject type involved, if any.
        method: HTTP method.
        path: Request path with ids replaced by ``:id``.
        request_id: Salesforce request id header, if present.
        retryable: Whether the error is transient in principle.
        errors: Every error entry when Salesforce returned several.
        raw: Original response body, for debugging (never logged by the library).
        tag: Custom error tag parsed from the message (``"[TAG] ..."``), if any.
        hint: A likely fix, computed from data the client already had (never an API call).
            ``None`` when the library has nothing specific to say.
        docs_url: Troubleshooting entry for this error class on the docs site.
    """

    default_retryable: bool = False
    # Class-level fallbacks, so errors pickled before 0.2 still format.
    hint: str | None = None
    docs_url: str | None = None

    message: str
    error_code: str | None
    fields: tuple[str, ...]
    status: int | None
    operation: str | None
    sobject: str | None
    method: str | None
    path: str | None
    request_id: str | None
    retryable: bool
    errors: tuple[ErrorDetail, ...]
    raw: bytes | str | None
    tag: str | None

    def __init__(
        self,
        message: str = "",
        *,
        error_code: str | None = None,
        fields: tuple[str, ...] | list[str] = (),
        status: int | None = None,
        operation: str | None = None,
        sobject: str | None = None,
        method: str | None = None,
        path: str | None = None,
        request_id: str | None = None,
        retryable: bool | None = None,
        errors: tuple[ErrorDetail, ...] | list[ErrorDetail] = (),
        raw: bytes | str | None = None,
        tag: str | None = None,
        hint: str | None = None,
        docs_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.fields = tuple(fields)
        self.status = status
        self.operation = operation
        self.sobject = sobject
        self.method = method
        self.path = path
        self.request_id = request_id
        self.retryable = self.default_retryable if retryable is None else retryable
        self.errors = tuple(errors)
        self.raw = raw
        self.tag = tag
        self.hint = hint
        self.docs_url = docs_url if docs_url is not None else docs_url_for(type(self))

    def __str__(self) -> str:
        head = f"{self.error_code}: {self.message}" if self.error_code else self.message
        ctx = [p for p in (self.operation, self.sobject) if p]
        if self.status is not None:
            ctx.append(f"HTTP {self.status}")
        text = f"{head} ({', '.join(ctx)})" if ctx else head
        if self.hint:
            text += f"\n  hint: {self.hint}"
        if self.docs_url:
            text += f"\n  docs: {self.docs_url}"
        return text

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(message={self.message!r}, error_code={self.error_code!r}, "
            f"status={self.status!r}, operation={self.operation!r})"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild, (type(self), dict(self.__dict__)))


# --- transport ---------------------------------------------------------------------------


class TransportError(SalesforceError):
    """No usable HTTP response was received."""

    default_retryable = True


class ConnectError(TransportError):
    """The connection could not be established; the request was never sent."""


class RequestTimeout(TransportError):
    """The request timed out."""


# --- auth --------------------------------------------------------------------------------


class AuthError(SalesforceError):
    """Authentication or token problem."""


class SessionExpired(AuthError):
    """401 that survived one refresh, or an expired :class:`StaticToken`."""


class ReauthorizationRequired(AuthError):
    """The refresh token is expired, revoked or rotated out; the user must log in again."""


class RefreshFailed(AuthError):
    """Token refresh failed. ``ambiguous=True`` when the outcome is unknown."""

    ambiguous: bool

    def __init__(self, message: str = "", *, ambiguous: bool = False, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.ambiguous = ambiguous


class TokenConflict(AuthError):
    """Compare-and-swap failure in a token store. Normally handled internally."""


class OAuthError(AuthError):
    """Error returned by an OAuth endpoint (``{"error", "error_description"}``)."""

    error: str | None
    error_description: str | None

    def __init__(
        self,
        message: str = "",
        *,
        error: str | None = None,
        error_description: str | None = None,
        **kw: Any,
    ) -> None:
        kw.setdefault("error_code", error)
        super().__init__(message, **kw)
        self.error = error
        self.error_description = error_description


# --- request -----------------------------------------------------------------------------


class RequestError(SalesforceError):
    """4xx caused by the request."""


class MalformedQuery(RequestError):
    """``MALFORMED_QUERY``, ``INVALID_QUERY_LOCATOR``, or a rejected cursor."""


class InvalidField(RequestError):
    """``INVALID_FIELD``, ``INVALID_FIELD_FOR_INSERT_UPDATE``, ..."""


class InvalidType(RequestError):
    """``INVALID_TYPE``: unknown sObject type."""


class NotFound(RequestError):
    """404 / ``NOT_FOUND`` / ``ENTITY_IS_DELETED``."""


class Forbidden(RequestError):
    """``INSUFFICIENT_ACCESS*``, ``API_DISABLED_FOR_ORG``, ..."""


class MultipleMatches(RequestError):
    """HTTP 300: an external id matched several records. See ``.matches``."""

    matches: tuple[str, ...]

    def __init__(self, message: str = "", *, matches: tuple[str, ...] = (), **kw: Any) -> None:
        super().__init__(message, **kw)
        self.matches = tuple(matches)


class Conflict(RequestError):
    """409 / 412 / 428."""


class Gone(RequestError):
    """410: resource retired or removed."""


class RequestTooLarge(RequestError):
    """414 / 431: URI or headers too large."""


class UnsupportedApiVersion(RequestError):
    """The API version doesn't fit the call. Two sources, told apart by ``error_code``:

    - ``"UNSUPPORTED_API_VERSION"``: Salesforce rejected the configured ``api_version`` (e.g. the
      org doesn't offer it yet).
    - ``"API_VERSION_TOO_OLD"``: raised by the library before sending, because a feature needs
      a newer ``api_version`` than configured.
    """


# --- record (DML) ------------------------------------------------------------------------


class RecordError(SalesforceError):
    """DML rejected by the org."""


class ValidationRuleFailed(RecordError):
    """``FIELD_CUSTOM_VALIDATION_EXCEPTION``: validation rules, Flow custom errors, ``addError()``."""


class RequiredFieldMissing(RecordError):
    """``REQUIRED_FIELD_MISSING``."""


class DuplicateDetected(RecordError):
    """``DUPLICATES_DETECTED``. ``.duplicates`` holds the duplicate result when provided."""

    duplicates: Any

    def __init__(self, message: str = "", *, duplicates: Any = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.duplicates = duplicates


class InvalidReference(RecordError):
    """``INVALID_CROSS_REFERENCE_KEY``, ``FIELD_INTEGRITY_EXCEPTION``."""


class StringTooLong(RecordError):
    """``STRING_TOO_LONG``."""


class InvalidPicklistValue(RecordError):
    """``INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST``."""


class RowLocked(RecordError):
    """``UNABLE_TO_LOCK_ROW`` (retryable)."""

    default_retryable = True


class RecordSaveBlocked(RecordError):
    """``CANNOT_INSERT_UPDATE_ACTIVATE_ENTITY``; in practice also unhandled trigger exceptions."""


class FlowFailed(RecordError):
    """``CANNOT_EXECUTE_FLOW_TRIGGER``."""


class StorageLimitExceeded(RecordError):
    """``STORAGE_LIMIT_EXCEEDED``."""


class EventPublishFailed(RecordError):
    """Salesforce rejected a platform event publish (``EventBus`` error).

    ``error_code`` tells you why, for example ``LIMIT_EXCEEDED`` (the hourly publishing
    allocation is used up) or ``PLATFORM_EVENT_PUBLISH_FAILED``.
    """


class EventPublishUnavailable(EventPublishFailed):
    """``PLATFORM_EVENT_PUBLISHING_UNAVAILABLE``: publishing is down for a moment (retryable)."""

    default_retryable = True


# --- limits ------------------------------------------------------------------------------


class LimitError(SalesforceError):
    """A Salesforce or library-side limit was hit."""


class RequestLimitExceeded(LimitError):
    """``REQUEST_LIMIT_EXCEEDED`` (HTTP 403) or HTTP 429. ``.retry_after`` in seconds if known."""

    retry_after: float | None

    def __init__(self, message: str = "", *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class BudgetExceeded(LimitError):
    """Raised by the library's own budget guard. ``.usage`` holds the current snapshot."""

    usage: UsageSnapshot | None

    def __init__(self, message: str = "", *, usage: UsageSnapshot | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.usage = usage


# --- others ------------------------------------------------------------------------------


class ServerError(SalesforceError):
    """500 / 502 / 503 / ``SERVER_UNAVAILABLE``."""


class PartialFailure(SalesforceError):
    """Some records of an ``allOrNone=false`` batch failed. ``.results`` holds the batch."""

    results: BatchResult[Any] | None

    def __init__(self, message: str = "", *, results: BatchResult[Any] | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.results = results


class BulkJobFailed(SalesforceError):
    """A Bulk 2.0 job ended in state ``Failed``."""

    job_id: str | None
    state: str | None
    error_message: str | None

    def __init__(
        self,
        message: str = "",
        *,
        job_id: str | None = None,
        state: str | None = None,
        error_message: str | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(message, **kw)
        self.job_id = job_id
        self.state = state
        self.error_message = error_message


class ApexError(SalesforceError):
    """``APEX_ERROR`` or an error returned by an Apex REST class. ``.body`` holds the body."""

    body: Any

    def __init__(self, message: str = "", *, body: Any = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.body = body


class GraphQLError(SalesforceError):
    """GraphQL response with an ``errors`` array. ``.graphql_errors`` holds it."""

    graphql_errors: tuple[Mapping[str, Any], ...]

    def __init__(
        self, message: str = "", *, graphql_errors: tuple[Mapping[str, Any], ...] = (), **kw: Any
    ) -> None:
        super().__init__(message, **kw)
        self.graphql_errors = tuple(graphql_errors)


class SoapFault(SalesforceError):
    """A SOAP fault that could not be mapped to a more specific class."""

    fault_code: str | None

    def __init__(self, message: str = "", *, fault_code: str | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.fault_code = fault_code


class UnexpectedResponse(SalesforceError):
    """HTML pages (proxy, maintenance), non-JSON bodies or schema surprises."""
