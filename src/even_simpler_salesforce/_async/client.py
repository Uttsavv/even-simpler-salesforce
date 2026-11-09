"""The ``AsyncSalesforce`` client (§4, §5.2); ``Salesforce`` is generated from it."""

from __future__ import annotations

import hashlib
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
from ..core.cursor import Cursor, decode_cursor, encode_cursor
from ..core.http import RawResponse, RequestSpec, check_sobject
from ..core.records import apply_model
from ..errors import SalesforceError, UnexpectedResponse
from ..models import Limit, QueryResult
from ..observability.debug import DebugPrinter, debug_level
from ..observability.events import Observer, ObserverHub
from ..usage import UsageTracker
from . import _compat
from .api.auth_api import AsyncAuthApi
from .api.collections import AsyncCollections
from .api.sobject import AsyncSObject
from .auth_runtime import AsyncAuthRuntime
from .token_store import AsyncInMemoryTokenStore
from .transport import AsyncTransport

__all__ = ["DEFAULT_API_VERSION", "MIN_API_VERSION", "AsyncSalesforce"]

DEFAULT_API_VERSION = "68.0"
MIN_API_VERSION = (59, 0)
MIN_LIMITS_POLL_INTERVAL = 300.0

M = TypeVar("M")
_FROM = re.compile(r"\bFROM\s+([A-Za-z][A-Za-z0-9_]*)", re.IGNORECASE)
_CLIENT_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_VERSION = re.compile(r"^\d{2,3}\.\d$")


def _sobject_of(soql: str) -> str | None:
    m = _FROM.search(soql)
    return m.group(1) if m else None


def _check_version(v: str) -> str:
    if not _VERSION.match(v):
        raise ValueError(f"api_version must look like '68.0', got {v!r}")
    major, minor = (int(x) for x in v.split("."))
    if (major, minor) < MIN_API_VERSION:
        raise ValueError(f"api_version {v} is older than the minimum supported v59.0")
    return v


class AsyncSalesforce:
    """Async Salesforce client.

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
        instance_url: Override the instance URL learned from the token response.
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
        if client_name is not None and not _CLIENT_NAME.match(client_name):
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
        self._runtime = AsyncAuthRuntime(auth, self._transport, self._store)
        self._caller: str | None = None
        self._priority: Priority | None = None
        self._root = True
        self._poller: _compat.Periodic | None = None
        self._init_areas()

    def _init_areas(self) -> None:
        self.collections = AsyncCollections(self)
        self.auth = AsyncAuthApi(self)

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
        child._poller = None
        child._init_areas()
        return child

    def __repr__(self) -> str:
        tok = self._runtime.current()
        where = tok.instance_url if tok else (self._transport.instance_url or "not connected")
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
        if self._poller is not None:
            await self._poller.stop()
            self._poller = None
        await self._transport.aclose()

    # --- family ------------------------------------------------------------------------

    def with_auth(self, auth: Auth, *, store: AsyncTokenStore | None = None) -> AsyncSalesforce:
        """A child client with its own auth, sharing pool, caches, observers and usage."""
        runtime = AsyncAuthRuntime(auth, self._transport, store if store is not None else self._store)
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
        return self._transport.api_version

    @property
    def observers(self) -> ObserverHub:
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

    async def describe_global(self, *, cached: bool = True) -> dict[str, Any]:
        """``GET sobjects``: list of sObjects, served from the describe cache when fresh."""
        return await self._describe("sobjects", "global", cached, None)

    async def _describe(self, path: str, what: str, cached: bool, sobject: str | None) -> dict[str, Any]:
        t = self._transport
        cfg = t.describe_cache
        tok = await self._runtime.token()
        user = hashlib.sha256((tok.user_id or self._runtime.key).encode()).hexdigest()[:12]
        key = f"esf:describe:{tok.org_id or tok.instance_url}:{t.api_version}:{what}:{user}"
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
                operation="sobject.describe" if sobject else "describe_global",
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
        self, soql: str, *, page_size: int | None = None, include_deleted: bool = False, model: None = None
    ) -> QueryResult[dict[str, Any]]: ...
    @overload
    async def query(
        self, soql: str, *, page_size: int | None = None, include_deleted: bool = False, model: type[M]
    ) -> QueryResult[M]: ...
    async def query(
        self, soql: str, *, page_size: int | None = None, include_deleted: bool = False, model: Any = None
    ) -> QueryResult[Any]:
        """Run SOQL and return one page plus a cursor.

        Args:
            soql: The query; build it with ``esf.soql()`` to quote values safely.
            page_size: ``Sforce-Query-Options: batchSize`` (200-2000; a hint, not a guarantee).
            include_deleted: Use ``queryAll`` (deleted and archived rows).
            model: Optional Pydantic model / dataclass for the rows.
        """
        return self._page(await self._query_body(soql, page_size, include_deleted), model)

    async def query_more(
        self, cursor: Cursor | str, *, page_size: int | None = None, model: Any = None
    ) -> QueryResult[Any]:
        """Fetch the page after ``cursor``. Rejects tampered or foreign cursors."""
        path = decode_cursor(cursor, self._transport.cursor_key)
        return self._page(await self._locator_body(path, page_size), model)

    async def query_iter(
        self,
        soql: str,
        *,
        page_size: int | None = None,
        include_deleted: bool = False,
        model: Any = None,
        expand_children: bool = False,
    ) -> AsyncIterator[Any]:
        """Yield every row, fetching pages lazily.

        With ``expand_children=True``, child subquery results that were truncated are followed
        to completion before the parent row is yielded.
        """
        body: Any = await self._query_body(soql, page_size, include_deleted)
        while True:
            page = self._page(body, None)
            for rec in page.records:
                if expand_children:
                    await self._expand_children(rec, page_size)
                yield apply_model([rec], model)[0] if model is not None else rec
            nxt: Any = body.get("nextRecordsUrl")
            if page.done or not nxt:
                return
            decode_cursor(encode_cursor(nxt))  # validate the shape before following it
            body = await self._locator_body(nxt, page_size, soql)

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

    async def count(self, soql: str) -> int:
        """Run ``SELECT COUNT() ...`` and return ``totalSize``."""
        body: Any = await self._query_body(soql, None, False)
        total: Any = cast("dict[str, Any]", body).get("totalSize") if isinstance(body, dict) else None
        if not isinstance(total, int) or isinstance(total, bool):
            raise UnexpectedResponse("unexpected COUNT() response shape", raw=repr(body)[:4096])  # pyright: ignore[reportUnknownArgumentType]
        return total

    async def search(self, sosl: str) -> list[dict[str, Any]]:
        """``GET search?q=``. Returns the matching records."""
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
            )
        )
        body: Any = raw.json()
        if isinstance(body, dict):
            return list(body.get("searchRecords") or [])  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return list(body or [])  # pyright: ignore[reportUnknownArgumentType]

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
        Returns decoded JSON (``None`` for empty bodies, ``str`` for non-JSON bodies).
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
        return raw.content.decode(errors="replace")

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

        Each poll costs one API call. Stopped by ``aclose()``. The async client needs a running event loop.
        """
        if interval < MIN_LIMITS_POLL_INTERVAL:
            raise ValueError("interval must be at least 300 seconds")
        if self._poller is None:
            self._poller = _compat.Periodic(self.limits, interval)
            self._poller.start()
