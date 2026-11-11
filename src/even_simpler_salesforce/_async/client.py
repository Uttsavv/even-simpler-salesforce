"""The ``AsyncSalesforce`` client (§4, §5.2); ``Salesforce`` is generated from it."""

from __future__ import annotations

import copy
import re
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from email.utils import formatdate
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, overload

if TYPE_CHECKING:
    import httpx

from ..auth.flows import Auth
from ..auth.store import AsyncTokenStore
from ..cache import Cache, MemoryCache
from ..config import Budget, DescribeCacheConfig, Priority, RetryPolicy, Timeouts
from ..context import current_call_context
from ..core.actions import Action, action_inputs, action_result_type, build_action_result
from ..core.cursor import Cursor, decode_cursor, encode_cursor
from ..core.http import RawResponse, RequestSpec, check_sobject
from ..core.records import apply_model, parse_query_plans
from ..core.soql import soql_values
from ..errors import MultipleMatches, NotFound, SalesforceError, UnexpectedResponse
from ..models import Limit, MergeResult, QueryPlan, QueryResult
from ..observability.debug import DebugPrinter, debug_level
from ..observability.events import Observer, ObserverHub
from ..usage import UsageTracker
from . import _compat
from .api.actions import AsyncActions
from .api.apex import AsyncApex
from .api.auth_api import AsyncAuthApi
from .api.collections import AsyncCollections
from .api.files import AsyncFiles
from .api.graphql import graphql as _graphql
from .api.graphql import graphql_iter as _graphql_iter
from .api.leads import AsyncLeads
from .api.platform_events import AsyncPlatformEvents
from .api.soap import merge as _merge
from .api.sobject import AsyncSObject
from .api.ui import AsyncUi
from .auth_runtime import AsyncAuthRuntime
from .token_store import AsyncInMemoryTokenStore
from .transport import AsyncTransport

__all__ = ["DEFAULT_API_VERSION", "MIN_API_VERSION", "AsyncSalesforce"]

DEFAULT_API_VERSION = "68.0"
MIN_API_VERSION = (59, 0)
MIN_LIMITS_POLL_INTERVAL = 300.0

M = TypeVar("M")
R = TypeVar("R")
_FROM = re.compile(r"\bFROM\s+([A-Za-z][A-Za-z0-9_]*)", re.IGNORECASE)
_STRING_LIT = re.compile(r"'(?:\\.|[^'\\])*'")
_PARENS = re.compile(r"\([^()]*\)")
_CHARSET = re.compile(r"charset=\"?([A-Za-z0-9_.:-]+)", re.IGNORECASE)
_CLIENT_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_VERSION = re.compile(r"^\d{2,3}\.\d$")


def _sobject_of(soql: str) -> str | None:
    """The top-level ``FROM`` object; subqueries in parentheses and string literals are skipped."""
    text = _STRING_LIT.sub("''", soql)
    while True:
        stripped = _PARENS.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    m = _FROM.search(text)
    return m.group(1) if m else None


_TEXT_SUBTYPES = frozenset({"json", "xml", "javascript", "ecmascript", "x-www-form-urlencoded", "csv"})


def _is_text(content_type: str) -> bool:
    """Whether a body should be decoded: ``text/*``, JSON/XML (incl. ``+json``/``+xml``), or a charset."""
    media, _, params = content_type.lower().partition(";")
    if any(p.strip().startswith("charset=") for p in params.split(";")):
        return True
    kind, _, subtype = media.strip().partition("/")
    if kind == "text":
        return True
    suffix = subtype.rpartition("+")[2]
    return subtype in _TEXT_SUBTYPES or suffix in ("json", "xml")


def _check_version(v: str) -> str:
    if not _VERSION.fullmatch(v):
        raise ValueError(f"api_version must look like {DEFAULT_API_VERSION!r}, got {v!r}")
    major, minor = (int(x) for x in v.split("."))
    if (major, minor) < MIN_API_VERSION:
        minimum = ".".join(str(x) for x in MIN_API_VERSION)
        raise ValueError(f"api_version {v} is older than the minimum supported v{minimum}")
    return v


class AsyncSalesforce:
    """Salesforce REST client. The async and sync clients share this API; the sync one is generated.

    Example:
        auth = esf.ClientCredentials(domain="https://acme.my.salesforce.com",
                                     client_id="...", client_secret="...")
        async with esf.AsyncSalesforce(auth) as sf:
            lead = await sf.sobject("Lead").get("00Q...")

    Clients are safe to share across tasks. ``with_auth()`` and ``tagged()`` return cheap
    children that share the connection pool, caches, observers and usage counters.

    Args:
        auth: Login flow (``ClientCredentials``, ``WebServerFlow``, ``JwtBearer``, ``StaticToken``).
        api_version: Salesforce API version (pinned per library release; v59.0 or newer).
        http_client: Inject your own ``httpx.AsyncClient`` (never closed by the library).
        store: Token store. Defaults to an in-memory store shared by the family.
        cache: Describe cache (``Cache`` protocol). Defaults to an in-memory LRU.
        observers: Event observers (see ``esf.observability``).
        budget: API usage budget and guard.
        retry: Retry policy.
        timeout: HTTP timeouts (ignored when ``http_client`` is given).
        max_concurrency: Max in-flight requests for the whole family.
        user_agent: ``User-Agent`` header.
        json: Object with ``dumps(obj) -> bytes`` and ``loads(bytes)`` to replace the JSON codec.
        error_tags: Map of ``[TAG]`` message prefixes to custom error classes (§8.4).
        cursor_key: HMAC key used to sign query cursors.
        instance_url: Override the instance URL learned from the token response (this client
            and ``tagged()`` views; not ``with_auth()`` children).
        describe_cache: Describe-cache settings.
        log_soql: ``"hash"`` (default) logs only ``soql_hash``; ``"redacted"`` also logs the
            SOQL with literals replaced by ``?``.
        http2: Enable HTTP/2 on the owned client (needs the ``[http2]`` extra).
        client_name: Sent as ``Sforce-Call-Options: client=<name>`` on every API call so admins
            can attribute API usage to this integration (letters, digits, ``_ . : -``; max 64).
    """

    def __init__(
        self,
        auth: Auth,
        *,
        api_version: str = DEFAULT_API_VERSION,
        http_client: httpx.AsyncClient | None = None,
        store: AsyncTokenStore | None = None,
        cache: Cache | None = None,
        observers: Iterable[Observer] = (),
        budget: Budget | None = None,
        retry: RetryPolicy | None = None,
        timeout: Timeouts | None = None,
        max_concurrency: int | None = None,
        user_agent: str | None = None,
        json: Any = None,
        error_tags: Mapping[str, type[SalesforceError]] | None = None,
        cursor_key: bytes | None = None,
        instance_url: str | None = None,
        describe_cache: DescribeCacheConfig | None = None,
        log_soql: Literal["hash", "redacted"] = "hash",
        http2: bool = False,
        client_name: str | None = None,
    ) -> None:
        for tag, cls in (error_tags or {}).items():
            if not (isinstance(cls, type) and issubclass(cls, SalesforceError)):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError(f"error_tags[{tag!r}] must be a SalesforceError subclass")
        if client_name is not None and not _CLIENT_NAME.fullmatch(client_name):
            raise ValueError("client_name may only contain letters, digits, '_', '.', ':', '-' (max 64)")
        dc = describe_cache or DescribeCacheConfig()
        level = debug_level()
        hub = ObserverHub(observers)
        if level:
            hub.add(DebugPrinter(level))
        self._transport = AsyncTransport(
            api_version=_check_version(api_version),
            http_client=http_client,
            timeout=timeout or Timeouts(),
            retry=retry or RetryPolicy(),
            hub=hub,
            usage=UsageTracker(budget),
            cache=cache if cache is not None else MemoryCache(dc.max_entries),
            describe_cache=dc,
            error_tags=error_tags,
            cursor_key=cursor_key,
            max_concurrency=max_concurrency,
            user_agent=user_agent,
            json=json,
            log_soql=log_soql,
            instance_url=instance_url,
            http2=http2,
            debug=level,
            client_name=client_name,
        )
        self._store: AsyncTokenStore = store if store is not None else AsyncInMemoryTokenStore()
        self._runtime = AsyncAuthRuntime(auth, self._transport, self._store, instance_url=instance_url)
        self._caller: str | None = None
        self._priority: Priority | None = None
        self._root = True
        self._init_areas()

    def _init_areas(self) -> None:
        self.collections = AsyncCollections(self)
        self.auth = AsyncAuthApi(self)
        self.actions = AsyncActions(self)
        self.apex = AsyncApex(self)
        self.leads = AsyncLeads(self)
        self.platform_events = AsyncPlatformEvents(self)
        self.files = AsyncFiles(self)
        self.ui = AsyncUi(self)

    @classmethod
    def _child(
        cls,
        parent: AsyncSalesforce,
        runtime: AsyncAuthRuntime,
        caller: str | None,
        priority: Priority | None,
    ) -> AsyncSalesforce:
        child = cls.__new__(cls)
        child._transport = parent._transport
        child._store = runtime.store
        child._runtime = runtime
        child._caller = caller
        child._priority = priority
        child._root = False
        child._init_areas()
        return child

    def __repr__(self) -> str:
        tok = self._runtime.current()
        where = self._runtime.instance_url or (tok.instance_url if tok else "not connected")
        return (
            f"<{type(self).__name__} {where} v{self._transport.api_version} auth={self._runtime.auth.kind}>"
        )

    # --- lifecycle ---------------------------------------------------------------------

    async def __aenter__(self) -> AsyncSalesforce:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the owned HTTP client. No-op on children and for injected clients."""
        if not self._root:
            return
        await self._transport.aclose()

    # --- family ------------------------------------------------------------------------

    def with_auth(
        self,
        auth: Auth,
        *,
        store: AsyncTokenStore | None = None,
        instance_url: str | None = None,
    ) -> AsyncSalesforce:
        """A child client with its own auth, sharing pool, caches, observers and usage.

        The parent's ``instance_url=`` override is not inherited (the child may be another
        org); pass ``instance_url=`` here if the child needs one.
        """
        runtime = AsyncAuthRuntime(
            auth, self._transport, store if store is not None else self._store, instance_url=instance_url
        )
        return type(self)._child(self, runtime, self._caller, self._priority)

    def tagged(self, caller: str | None = None, priority: Priority | None = None) -> AsyncSalesforce:
        """A child view whose calls carry ``caller`` / ``priority`` (for logs, counters, budget)."""
        return type(self)._child(self, self._runtime, caller or self._caller, priority or self._priority)

    @property
    def usage(self) -> UsageTracker:
        """API usage tracker; ``sf.usage.snapshot()`` returns a :class:`UsageSnapshot`."""
        return self._transport.usage

    @property
    def api_version(self) -> str:
        """The Salesforce API version used for every URL (e.g. ``"68.0"``)."""
        return self._transport.api_version

    @property
    def instance_url(self) -> str | None:
        """Base URL of the org this client talks to (e.g. ``https://acme.my.salesforce.com``).

        The ``instance_url=`` override if one was given, else the instance of the current token;
        ``None`` before the first call (no token yet). Read-only; no I/O. Use it to build links;
        make calls with :meth:`request`.
        """
        if self._runtime.instance_url:
            return self._runtime.instance_url
        tok = self._runtime.current()
        return tok.instance_url if tok is not None else None

    @property
    def observers(self) -> ObserverHub:
        """The observer hub shared by the client family; ``sf.observers.add(obs)`` adds one."""
        return self._transport.hub

    # --- pipeline ----------------------------------------------------------------------

    async def _send(self, spec: RequestSpec) -> RawResponse:
        ctx = current_call_context()
        caller = self._caller or ctx.caller
        priority = spec.priority or self._priority or ctx.priority or "normal"
        return await self._transport.send(spec, self._runtime, caller=caller, priority=priority)

    # --- records -----------------------------------------------------------------------

    def sobject(self, name: str) -> AsyncSObject:
        """Operations on one sObject type, e.g. ``sf.sobject("Lead").get(id)``."""
        return AsyncSObject(self, name)

    async def merge(self, master_id: str, duplicate_ids: Sequence[str], *, sobject: str) -> MergeResult:
        """Merge up to two records into a master record (SOAP ``merge``; REST has none).

        Salesforce deletes the merged records and moves their related records to the master.

        Args:
            master_id: The record to keep.
            duplicate_ids: One or two records of the same object to merge into the master.
            sobject: ``"Lead"``, ``"Contact"`` or ``"Account"``.

        Returns:
            The master id, the merged ids and the related records that moved.

        Raises:
            ValueError: unsupported ``sobject``, malformed ids, or not 1 to 2 distinct duplicates.
            RecordError: Salesforce refused the merge; the subclass follows the status code.
        """
        return await _merge(self, master_id, duplicate_ids, sobject)

    async def describe_global(self, *, cached: bool = True) -> dict[str, Any]:
        """``GET sobjects``: list of sObjects, served from the describe cache when fresh."""
        return await self._describe("sobjects", "global", cached, None)

    async def _describe(
        self, path: str, what: str, cached: bool, sobject: str | None, operation: str | None = None
    ) -> dict[str, Any]:
        # Callers get their own copy: the cached dict is shared by the whole family.
        # ``operation`` names other cached metadata reads (sf.ui); it defaults to the describe ones.
        t = self._transport
        tok = await self._runtime.token()
        key = t.describe_key(tok, self._runtime.key, what)
        op = operation or ("sobject.describe" if sobject else "describe_global")
        if not cached:
            return copy.deepcopy(await self._describe_fetch(path, key, False, sobject, op))
        # Single flight: concurrent misses for one key wait for the first fetch, then hit the cache.
        async with t.single_flight(key):
            return copy.deepcopy(await self._describe_fetch(path, key, True, sobject, op))

    async def _describe_fetch(
        self, path: str, key: str, cached: bool, sobject: str | None, operation: str
    ) -> Any:
        t = self._transport
        cfg = t.describe_cache
        cached_value: Any = t.cache.get(key) if cached else None
        entry: dict[str, Any] | None = (
            cast("dict[str, Any]", cached_value) if isinstance(cached_value, dict) else None
        )
        now = time.time()
        headers: dict[str, str] = {}
        if entry is not None:
            if now - float(entry.get("fetched_at", 0)) < cfg.ttl:  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                return entry["data"]  # pyright: ignore[reportUnknownVariableType]
            if cfg.revalidate and entry.get("last_modified"):  # pyright: ignore[reportUnknownMemberType]
                headers["If-Modified-Since"] = str(entry["last_modified"])  # pyright: ignore[reportUnknownArgumentType]
        raw = await self._send(
            RequestSpec(
                "GET",
                path,
                headers=headers,
                operation=operation,
                sobject=sobject,
                allow_not_modified=bool(headers),
            )
        )
        if raw.status == 304 and entry is not None:
            data: Any = entry["data"]
        else:
            data = raw.json()
        last_modified = raw.headers.get("last-modified") or formatdate(now, usegmt=True)
        if raw.status == 304 and entry is not None:
            last_modified = str(entry.get("last_modified") or last_modified)
        t.cache.set(key, {"data": data, "last_modified": last_modified, "fetched_at": now}, cfg.stale_ttl)
        return data

    # --- query & search ----------------------------------------------------------------

    def _page(self, body: Any, model: Any) -> QueryResult[Any]:
        if not isinstance(body, dict) or not isinstance(body.get("records"), list):  # pyright: ignore[reportUnknownMemberType]
            raise UnexpectedResponse("unexpected query response shape")
        page = cast("dict[str, Any]", body)
        nxt: str | None = page.get("nextRecordsUrl")
        done = bool(page.get("done", True))
        cursor = encode_cursor(nxt, self._transport.cursor_key) if (nxt and not done) else None
        return QueryResult(
            records=apply_model(page["records"], model),
            total_size=int(page.get("totalSize", 0)),
            done=done,
            cursor=cursor,
        )

    @staticmethod
    def _query_headers(page_size: int | None) -> dict[str, str]:
        if page_size is None:
            return {}
        if not 200 <= page_size <= 2000:
            raise ValueError("page_size must be between 200 and 2000")
        return {"Sforce-Query-Options": f"batchSize={page_size}"}

    async def _query_body(self, soql: str, page_size: int | None, include_deleted: bool) -> Any:
        raw = await self._send(
            RequestSpec(
                "GET",
                "queryAll" if include_deleted else "query",
                params={"q": soql},
                headers=self._query_headers(page_size),
                idempotent=True,
                operation="query",
                sobject=_sobject_of(soql),
                soql=soql,
            )
        )
        return raw.json()

    async def _locator_body(self, path: str, page_size: int | None, soql: str | None = None) -> Any:
        raw = await self._send(
            RequestSpec(
                "GET",
                path,
                headers=self._query_headers(page_size),
                idempotent=True,
                operation="query_more",
                sobject=_sobject_of(soql) if soql else None,
                soql=soql,
            )
        )
        return raw.json()

    @overload
    async def query(
        self,
        soql: str,
        *values: Any,
        page_size: int | None = None,
        include_deleted: bool = False,
        model: None = None,
    ) -> QueryResult[dict[str, Any]]: ...
    @overload
    async def query(
        self,
        soql: str,
        *values: Any,
        page_size: int | None = None,
        include_deleted: bool = False,
        model: type[M],
    ) -> QueryResult[M]: ...
    async def query(
        self,
        soql: str,
        *values: Any,
        page_size: int | None = None,
        include_deleted: bool = False,
        model: Any = None,
    ) -> QueryResult[Any]:
        """Run SOQL and return one page plus a cursor.

        Example:
            page = await sf.query("SELECT Id FROM Lead WHERE Email = {}", email)

        Args:
            soql: The query. ``{}`` placeholders are filled with ``values``, quoted safely.
            *values: Values for the ``{}`` placeholders (same quoting as ``esf.soql``). Without
                values the query is sent unchanged.
            page_size: ``Sforce-Query-Options: batchSize`` (200-2000; a hint, not a guarantee).
            include_deleted: Use ``queryAll`` (deleted and archived rows).
            model: Optional Pydantic model / dataclass for the rows.
        """
        soql = soql_values(soql, values)
        return self._page(await self._query_body(soql, page_size, include_deleted), model)

    async def query_one(
        self, soql: str, *values: Any, include_deleted: bool = False, model: Any = None
    ) -> Any:
        """Return exactly one row.

        Reads only the first page and decides from ``totalSize``; the SOQL is never rewritten.

        Raises:
            NotFound: no rows.
            MultipleMatches: more than one row (``.matches`` holds the Ids on the first page).
        """
        soql = soql_values(soql, values)
        page = self._page(await self._query_body(soql, None, include_deleted), None)
        sobject = _sobject_of(soql)
        if page.total_size == 0 or not page.records:
            raise NotFound("query returned no rows; expected exactly one", operation="query", sobject=sobject)
        if page.total_size > 1:
            rows = cast("list[dict[str, Any]]", page.records)
            ids = tuple(str(r["Id"]) for r in rows if r.get("Id"))
            raise MultipleMatches(
                f"query returned {page.total_size} rows; expected exactly one",
                matches=ids,
                operation="query",
                sobject=sobject,
            )
        return apply_model(page.records[:1], model)[0]

    async def query_first(
        self, soql: str, *values: Any, include_deleted: bool = False, model: Any = None
    ) -> Any:
        """Return the first row, or ``None`` when there are none (never raises for no rows).

        Reads only the first page; add ``LIMIT 1`` yourself to avoid fetching more rows.
        """
        soql = soql_values(soql, values)
        page = self._page(await self._query_body(soql, None, include_deleted), None)
        return apply_model(page.records[:1], model)[0] if page.records else None

    async def explain(self, soql: str, *values: Any) -> list[QueryPlan]:
        """``GET query?explain=``: how Salesforce would run ``soql``, without running it.

        Returns the plans as Salesforce sends them (most optimal first). Salesforce labels the
        explain parameter a Beta feature.
        """
        soql = soql_values(soql, values)
        raw = await self._send(
            RequestSpec(
                "GET",
                "query",
                params={"explain": soql},
                idempotent=True,
                operation="query.explain",
                sobject=_sobject_of(soql),
                soql=soql,
            )
        )
        return parse_query_plans(raw.json())

    async def query_more(
        self, cursor: Cursor | str, *, page_size: int | None = None, model: Any = None
    ) -> QueryResult[Any]:
        """Fetch the page after ``cursor``. Rejects tampered or foreign cursors."""
        path = decode_cursor(cursor, self._transport.cursor_key)
        return self._page(await self._locator_body(path, page_size), model)

    async def query_iter(
        self,
        soql: str,
        *values: Any,
        page_size: int | None = None,
        include_deleted: bool = False,
        model: Any = None,
        expand_children: bool = False,
    ) -> AsyncIterator[Any]:
        """Yield every row, fetching pages lazily.

        ``values`` fill the ``{}`` placeholders as in :meth:`query`. With
        ``expand_children=True``, child subquery results that were truncated are followed to
        completion before the parent row is yielded.
        """
        soql = soql_values(soql, values)
        body: Any = await self._query_body(soql, page_size, include_deleted)
        while True:
            page = self._page(body, None)
            for rec in page.records:
                if expand_children:
                    await self._expand_children(rec, page_size)
                yield apply_model([rec], model)[0] if model is not None else rec
            if page.cursor is None:  # _page validated nextRecordsUrl when it built the cursor
                return
            body = await self._locator_body(str(body["nextRecordsUrl"]), page_size, soql)

    async def _expand_children(self, record: dict[str, Any], page_size: int | None) -> None:
        for value in record.values():
            if not isinstance(value, dict):
                continue
            child: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
            records = child.get("records")
            if not isinstance(records, list):
                continue
            while not child.get("done", True) and child.get("nextRecordsUrl"):
                nxt = str(child["nextRecordsUrl"])
                decode_cursor(encode_cursor(nxt))
                more: Any = await self._locator_body(nxt, page_size)
                records.extend(more.get("records") or [])  # pyright: ignore[reportUnknownMemberType]
                child["done"] = bool(more.get("done", True))
                child["nextRecordsUrl"] = more.get("nextRecordsUrl")
            child.pop("nextRecordsUrl", None)

    async def count(self, soql: str, *values: Any) -> int:
        """Run ``SELECT COUNT() ...`` and return ``totalSize``; ``values`` as in :meth:`query`."""
        soql = soql_values(soql, values)
        body: Any = await self._query_body(soql, None, False)
        total: Any = cast("dict[str, Any]", body).get("totalSize") if isinstance(body, dict) else None
        if not isinstance(total, int) or isinstance(total, bool):
            raise UnexpectedResponse("unexpected COUNT() response shape", raw=repr(body)[:4096])  # pyright: ignore[reportUnknownArgumentType]
        return total

    async def search(self, sosl: str) -> list[dict[str, Any]]:
        """``GET search?q=``. Returns the matching records.

        Wrap user input in :func:`esf.sosl_term` (``f"FIND {esf.sosl_term(text)} ..."``); SOQL
        quoting does not apply to SOSL search terms.
        """
        raw = await self._send(
            RequestSpec("GET", "search", params={"q": sosl}, idempotent=True, operation="search", soql=sosl)
        )
        body: Any = raw.json()
        if isinstance(body, dict):
            return list(body.get("searchRecords") or [])  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return list(body or [])  # pyright: ignore[reportUnknownArgumentType]

    async def parameterized_search(
        self,
        q: str,
        *,
        sobjects: Sequence[str | Mapping[str, Any]] | None = None,
        fields: Sequence[str] | None = None,
        limit: int | None = None,
        **extra: Any,
    ) -> list[dict[str, Any]]:
        """``POST parameterizedSearch``. ``sobjects`` items may be names or full spec dicts."""
        payload: dict[str, Any] = {"q": q, **extra}
        if sobjects:
            payload["sobjects"] = [
                {"name": check_sobject(s)} if isinstance(s, str) else dict(s) for s in sobjects
            ]
        if fields:
            payload["fields"] = list(fields)
        if limit is not None:
            payload["overallLimit"] = limit
        content, ctype = self._transport.encode(payload)
        raw = await self._send(
            RequestSpec(
                "POST",
                "parameterizedSearch",
                content=content,
                content_type=ctype,
                idempotent=True,
                operation="parameterized_search",
                soql=q,
            )
        )
        body: Any = raw.json()
        if isinstance(body, dict):
            return list(body.get("searchRecords") or [])  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return list(body or [])  # pyright: ignore[reportUnknownArgumentType]

    # --- graphql -----------------------------------------------------------------------

    async def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """``POST graphql``: run a GraphQL query and return its ``data`` member.

        Example:
            data = await sf.graphql(
                "query Lead($id: ID) { uiapi { query { Lead(where: {Id: {eq: $id}}) "
                "{ edges { node { Id Name { value } } } } } } }",
                {"id": lead_id},
            )

        Queries are reads, so the client resends them after a timeout or a 5xx. A document
        with a ``mutation`` is never resent after it may have reached Salesforce.

        Args:
            query: The GraphQL document.
            variables: Values for the variables the document declares.

        Raises:
            GraphQLError: the response has an ``errors`` array, even with ``200 OK``.
                ``.data`` holds any partial data that came with the errors.
        """
        return await _graphql(self, query, variables)

    async def graphql_iter(
        self, query: str, variables: Mapping[str, Any] | None = None, *, path: str
    ) -> AsyncIterator[Any]:
        """Yield every node of a GraphQL connection, fetching pages lazily.

        Example:
            async for lead in sf.graphql_iter(query, path="uiapi.query.Lead"):
                print(lead["Id"])

        The query must declare an ``$after`` variable and pass it to the connection, and must
        select ``pageInfo { hasNextPage endCursor }``. The client sets ``after`` to
        ``endCursor`` for each next page and yields each ``edges[].node``.

        Args:
            query: A GraphQL document with exactly one query that declares ``$after``.
            variables: Values for the other variables (an ``after`` here sets the first cursor).
            path: Dotted path from ``data`` to the connection, e.g. ``"uiapi.query.Lead"``.

        Raises:
            ValueError: the query doesn't declare ``$after``, or ``path`` is malformed.
            GraphQLError: a page has an ``errors`` array.
            UnexpectedResponse: ``path`` isn't in the response or lacks ``edges``/``pageInfo``.
        """
        async for node in _graphql_iter(self, query, variables, path):
            yield node

    # --- org automation --------------------------------------------------------------

    async def run(self, action: Action[R]) -> R:
        """Run a typed action stub (written by ``esf generate actions``) and return its result.

        Example:
            result = await sf.run(AssignLead(lead_id="00Q..."))

        The inputs are the stub's fields; fields set to ``None`` are not sent. The output values
        become the stub's result type (a dataclass or a Pydantic model).

        Raises:
            SalesforceError: the typed error of the failed invocation, as for
                ``sf.actions.flow(name, {...})``.
        """
        kind, name, inputs = action_inputs(action)
        outputs = await self.actions._invoke(kind, name, inputs)
        result: R = build_action_result(action_result_type(type(action)), outputs)
        return result

    # --- raw & limits ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
        operation: str = "raw",
    ) -> Any:
        """Call any REST resource through the full pipeline (auth, refresh, retries, errors, logs).

        ``path`` is relative to ``/services/data/vXX.X/`` or absolute under ``/services/``.
        Returns decoded JSON, ``None`` for an empty body, ``str`` for a text body (``text/*``,
        XML, or any ``charset=``) and ``bytes`` for anything else, such as
        ``ContentVersion/{id}/VersionData`` file contents.
        """
        ctype: str | None = None
        if json is not None and content is not None:
            raise ValueError("pass json= or content=, not both")
        if method.upper() in ("GET", "HEAD") and (json is not None or content is not None):
            raise ValueError(f"{method.upper()} requests can't carry a body")
        if json is not None:
            content, ctype = self._transport.encode(json)
        h = dict(headers or {})
        # Header names are case-insensitive: a Content-Type the caller passes (in any spelling)
        # replaces the default instead of being sent alongside it.
        given = [h.pop(k) for k in [k for k in h if k.lower() == "content-type"]]
        if content is not None:
            ctype = (given[-1] if given else None) or ctype or "application/octet-stream"
        raw = await self._send(
            RequestSpec(
                method.upper(),
                path,
                params=params,
                content=content,
                content_type=ctype,
                headers=h,
                idempotent=idempotent,
                operation=operation,
                expect_json=False,
            )
        )
        if not raw.content:
            return None
        if raw.is_json:
            return raw.json()
        ctype = raw.headers.get("content-type", "").strip()  # with parameters (charset)
        if ctype:
            if not _is_text(ctype):
                return raw.content
            m = _CHARSET.search(ctype)
            try:
                return raw.content.decode(m.group(1) if m else "utf-8", errors="replace")
            except LookupError:  # unknown charset name
                return raw.content.decode(errors="replace")
        try:
            return raw.content.decode()
        except UnicodeDecodeError:
            return raw.content  # no content type and not UTF-8: binary

    async def limits(self) -> dict[str, Limit]:
        """``GET limits``: every org limit. Costs one API call; never called implicitly."""
        raw = await self._send(RequestSpec("GET", "limits", idempotent=True, operation="limits"))
        body: Any = raw.json()
        out: dict[str, Limit] = {}
        if isinstance(body, dict):
            for name, v in body.items():  # pyright: ignore[reportUnknownVariableType]
                if isinstance(v, dict) and "Max" in v and "Remaining" in v:
                    details = {k: x for k, x in v.items() if k not in ("Max", "Remaining")}  # pyright: ignore[reportUnknownVariableType]
                    out[str(name)] = Limit(int(v["Max"]), int(v["Remaining"]), details)  # pyright: ignore[reportUnknownArgumentType]
        daily = out.get("DailyApiRequests")
        if daily is not None:
            from ..core.limits import LimitInfo

            self._transport.usage.observe(LimitInfo(daily.used, daily.max))
        return out

    def start_limits_poller(self, interval: float = MIN_LIMITS_POLL_INTERVAL) -> None:
        """Opt-in: poll ``GET limits`` every ``interval`` seconds (min 300) to refresh usage.

        Each poll costs one API call. Stopped when the root client closes.
        With ``AsyncSalesforce`` call this from inside a running event loop; ``Salesforce`` uses a
        daemon thread.
        """
        if interval < MIN_LIMITS_POLL_INTERVAL:
            raise ValueError("interval must be at least 300 seconds")
        t = self._transport
        if t.closed:
            raise RuntimeError("client is closed")
        if t.poller is None:  # one poller per family; stopped when the root client closes
            t.poller = _compat.Periodic(self.limits, interval)
            t.poller.start()
