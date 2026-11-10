"""even-simpler-salesforce: a modern, typed, async-first Salesforce client.

    import even_simpler_salesforce as esf

    async with esf.AsyncSalesforce(esf.ClientCredentials(...)) as sf:
        page = await sf.query(esf.soql("SELECT Id FROM Lead WHERE Email = {}", email))

``__all__`` is the SemVer-covered public surface. Everything else is private.
"""

from ._async.client import AsyncSalesforce
from ._async.token_store import AsyncInMemoryTokenStore
from ._sync.client import Salesforce
from ._sync.token_store import InMemoryTokenStore
from ._version import __version__
from .auth import (
    AsyncTokenStore,
    Auth,
    AuthStart,
    ClientCredentials,
    JwtBearer,
    StaticToken,
    Token,
    TokenStore,
    WebServerFlow,
)
from .cache import Cache, MemoryCache
from .config import Budget, DescribeCacheConfig, RetryPolicy, Timeouts
from .context import call_context
from .core.actions import Action
from .core.cursor import Cursor
from .core.records import flatten
from .core.soql import soql, soql_identifier, soql_like, sosl_term
from .errors import (
    ApexError,
    AuthError,
    BudgetExceeded,
    BulkJobFailed,
    Conflict,
    ConnectError,
    DuplicateDetected,
    ErrorDetail,
    FlowFailed,
    Forbidden,
    Gone,
    GraphQLError,
    InvalidField,
    InvalidPicklistValue,
    InvalidReference,
    InvalidType,
    LimitError,
    MalformedQuery,
    MultipleMatches,
    NotFound,
    OAuthError,
    PartialFailure,
    ReauthorizationRequired,
    RecordError,
    RecordSaveBlocked,
    RefreshFailed,
    RequestError,
    RequestLimitExceeded,
    RequestTimeout,
    RequestTooLarge,
    RequiredFieldMissing,
    RowLocked,
    SalesforceError,
    ServerError,
    SessionExpired,
    SoapFault,
    StorageLimitExceeded,
    StringTooLong,
    TokenConflict,
    TransportError,
    UnexpectedResponse,
    UnsupportedApiVersion,
    ValidationRuleFailed,
)
from .models import (
    ActionResult,
    BatchResult,
    Limit,
    QueryPlan,
    QueryPlanNote,
    QueryResult,
    RecordResult,
    UpsertResult,
    UsageSnapshot,
)
from .observability import Event, Observer, capture


class EsfDeprecationWarning(DeprecationWarning):
    """Raised for deprecated even-simpler-salesforce features (§17.1)."""


__all__ = [
    "Action",
    "ActionResult",
    "ApexError",
    "AsyncInMemoryTokenStore",
    "AsyncSalesforce",
    "AsyncTokenStore",
    "Auth",
    "AuthError",
    "AuthStart",
    "BatchResult",
    "Budget",
    "BudgetExceeded",
    "BulkJobFailed",
    "Cache",
    "ClientCredentials",
    "Conflict",
    "ConnectError",
    "Cursor",
    "DescribeCacheConfig",
    "DuplicateDetected",
    "ErrorDetail",
    "EsfDeprecationWarning",
    "Event",
    "FlowFailed",
    "Forbidden",
    "Gone",
    "GraphQLError",
    "InMemoryTokenStore",
    "InvalidField",
    "InvalidPicklistValue",
    "InvalidReference",
    "InvalidType",
    "JwtBearer",
    "Limit",
    "LimitError",
    "MalformedQuery",
    "MemoryCache",
    "MultipleMatches",
    "NotFound",
    "OAuthError",
    "Observer",
    "PartialFailure",
    "QueryPlan",
    "QueryPlanNote",
    "QueryResult",
    "ReauthorizationRequired",
    "RecordError",
    "RecordResult",
    "RecordSaveBlocked",
    "RefreshFailed",
    "RequestError",
    "RequestLimitExceeded",
    "RequestTimeout",
    "RequestTooLarge",
    "RequiredFieldMissing",
    "RetryPolicy",
    "RowLocked",
    "Salesforce",
    "SalesforceError",
    "ServerError",
    "SessionExpired",
    "SoapFault",
    "StaticToken",
    "StorageLimitExceeded",
    "StringTooLong",
    "Timeouts",
    "Token",
    "TokenConflict",
    "TokenStore",
    "TransportError",
    "UnexpectedResponse",
    "UnsupportedApiVersion",
    "UpsertResult",
    "UsageSnapshot",
    "ValidationRuleFailed",
    "WebServerFlow",
    "__version__",
    "call_context",
    "capture",
    "flatten",
    "soql",
    "soql_identifier",
    "soql_like",
    "sosl_term",
]
