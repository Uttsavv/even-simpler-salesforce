"""The request pipeline (§7.1). The only path to the network for API calls.

auth → budget guard → concurrency gate → compress → send → parse limits → classify
→ refresh/retry → emit one ``sf.request`` event.
"""

from __future__ import annotations

import contextlib
import random
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, TypeVar, cast

import httpx

from .._version import __version__
from ..cache import Cache
from ..config import DescribeCacheConfig, RetryPolicy, Timeouts
from ..core import jsonlib
from ..core.classify import classify
from ..core.http import (
    GZIP_THRESHOLD,
    LOOPBACK_MOUNTS,
    RawResponse,
    RequestSpec,
    build_url,
    check_instance_url,
    gzip_body,
    merge_call_options,
    path_template,
)
from ..core.limits import parse_limit_info
from ..core.retry import backoff_delay, decide_retry, resend_after_401
from ..core.soql import redact_soql, soql_hash
from ..errors import (
    ConnectError,
    RequestTimeout,
    SalesforceError,
    SessionExpired,
    TransportError,
)
from ..observability.events import ObserverHub
from ..observability.summary import record_event
from ..usage import UsageTracker
from . import _compat

if TYPE_CHECKING:
    from ..auth.token import Token
    from .auth_runtime import AsyncAuthRuntime

__all__ = ["DEFAULT_USER_AGENT", "AsyncTransport"]

DEFAULT_USER_AGENT = f"even-simpler-salesforce/{__version__}"
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)
_REDACT_HEADERS = frozenset({"authorization", "cookie", "set-cookie"})
#: In-memory tokens kept per client family; the least recently used beyond this are dropped.
MAX_TOKENS_IN_MEMORY = 10_000

_K = TypeVar("_K")
_V = TypeVar("_V")


class LruDict(OrderedDict[_K, _V]):
    """A dict that keeps at most ``maxsize`` entries, dropping the least recently used.

    Keys held with :meth:`pin` are never dropped. The dict may exceed ``maxsize`` while every
    candidate is pinned, and shrinks back when the pins are released. Pinning, lookups,
    inserts and eviction share one lock, so a key can't be evicted between being chosen and
    being pinned.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self.maxsize = maxsize
        self._pins: dict[_K, int] = {}
        self._guard = threading.RLock()

    def pin(self, key: _K) -> None:
        """Keep ``key`` (now, or once it is set) from being evicted until :meth:`unpin`. Counted."""
        with self._guard:
            self._pins[key] = self._pins.get(key, 0) + 1

    def unpin(self, key: _K) -> None:
        """Release one :meth:`pin` of ``key``; trims the dict back to ``maxsize`` when possible."""
        with self._guard:
            n = self._pins.get(key, 0) - 1
            if n > 0:
                self._pins[key] = n
                return
            self._pins.pop(key, None)
            self._trim(None)

    def get(self, key: _K, default: Any = None) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        with self._guard:
            if key not in self:
                return default
            self.move_to_end(key)
            return self[key]

    def __setitem__(self, key: _K, value: _V) -> None:
        with self._guard:
            super().__setitem__(key, value)
            self.move_to_end(key)
            self._trim(key)

    def pop(self, key: _K, *default: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        with self._guard:
            return super().pop(key, *default)

    def _trim(self, keep: _K | None) -> None:
        # Caller holds the guard. Oldest first; never the pinned keys or ``keep`` (just set).
        excess = len(self) - self.maxsize
        if excess <= 0:
            return
        victims: list[_K] = []
        for k in self:
            if k != keep and not self._pins.get(k):
                victims.append(k)
                if len(victims) >= excess:
                    break
        for k in victims:
            self.pop(k, None)


class _AttemptFailed(Exception):
    def __init__(self, error: TransportError, sent: bool) -> None:
        self.error = error
        self.sent = sent


def _failed_row(item: Any) -> bool:
    return isinstance(item, dict) and cast("dict[str, Any]", item).get("success") is False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AsyncTransport:
    """Shared state of one client family: HTTP pool, tokens, caches, observers, usage."""

    def __init__(
        self,
        *,
        api_version: str,
        http_client: httpx.AsyncClient | None,
        timeout: Timeouts,
        retry: RetryPolicy,
        hub: ObserverHub,
        usage: UsageTracker,
        cache: Cache,
        describe_cache: DescribeCacheConfig,
        error_tags: Mapping[str, type[SalesforceError]] | None,
        cursor_key: bytes | None,
        max_concurrency: int | None,
        user_agent: str | None,
        json: Any,
        log_soql: str,
        instance_url: str | None,
        http2: bool,
        debug: int,
        client_name: str | None = None,
    ) -> None:
        self.api_version = api_version
        if http_client is None:
            http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=timeout.connect, read=timeout.read, write=timeout.write, pool=timeout.pool
                ),
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0),
                http2=http2,
                headers={"Accept-Encoding": "gzip, deflate"},
                # Plain-http loopback (local proxies, test servers) always connects directly, so
                # an HTTP_PROXY from the environment never sees a bearer token in cleartext.
                mounts={f"http://{h}": httpx.AsyncHTTPTransport(http2=http2) for h in LOOPBACK_MOUNTS},
            )
            self.owns_http = True
        else:
            self.owns_http = False
        self.http = http_client
        self.retry = retry
        self.hub = hub
        self.usage = usage
        self.cache = cache
        self.describe_cache = describe_cache
        self.error_tags = dict(error_tags or {})
        self.cursor_key = cursor_key
        self.gate = _compat.Semaphore(max_concurrency) if max_concurrency else None
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.dumps: Callable[[Any], bytes] = getattr(json, "dumps", None) or jsonlib.dumps
        self.loads: Callable[[bytes | str], Any] = getattr(json, "loads", None) or jsonlib.loads
        self.log_soql = log_soql
        self.instance_url = check_instance_url(instance_url) if instance_url else None
        self.debug = debug
        self.call_options = f"client={client_name}" if client_name else None
        # Bounded so per-user clients (with_auth per request) don't keep every token in memory;
        # an evicted token is reloaded from the store on its next use.
        self.tokens: LruDict[str, Token] = LruDict(MAX_TOKENS_IN_MEMORY)
        # Per token key: (refresh generation, error of the latest refresh or None on success).
        self.refresh_outcomes: LruDict[str, tuple[int, SalesforceError | None]] = LruDict(
            MAX_TOKENS_IN_MEMORY
        )
        self._locks_guard = threading.Lock()
        # key -> [lock, holders + waiters]; dropped when the count reaches 0.
        self._flights: dict[str, list[Any]] = {}
        self._deferred: list[tuple[str, dict[str, Any]]] = []
        self.sleep: Callable[[float], Any] = _compat.sleep
        self.now: Callable[[], datetime] = _utcnow
        self.rng = random.Random()
        self.closed = False
        self.poller: _compat.Periodic | None = None
        usage.bind(self.emit)

    # --- helpers ---------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def single_flight(self, key: str) -> AsyncIterator[None]:
        """Serialise work for ``key``; the lock is retired once no caller holds or awaits it.

        Used per token key (refresh, acquisition) and per describe key; both can have
        unbounded cardinality, so idle locks must not pile up.
        """
        with self._locks_guard:
            entry = self._flights.get(key)
            if entry is None:
                entry = self._flights[key] = [_compat.Lock(), 0]
            entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            with self._locks_guard:
                entry[1] -= 1
                if entry[1] == 0 and self._flights.get(key) is entry:
                    del self._flights[key]

    def emit(self, name: str, fields: Mapping[str, Any]) -> None:
        event = self.hub.emit(name, fields)
        record_event(event)

    def emit_later(self, name: str, fields: Mapping[str, Any]) -> None:
        """Queue an event to be emitted by :meth:`flush_deferred` (used under locks)."""
        with self._locks_guard:
            self._deferred.append((name, dict(fields)))

    def flush_deferred(self) -> None:
        with self._locks_guard:
            pending, self._deferred = self._deferred, []
        for name, fields in pending:
            self.emit(name, fields)

    def encode(self, obj: Any) -> tuple[bytes, str]:
        return self.dumps(obj), "application/json"

    async def post_form(self, url: str, form: dict[str, str]) -> RawResponse:
        """POST a form to an OAuth endpoint (token, introspect, revoke).

        No bearer token and no retries: the auth runtime owns those semantics (§6.4).
        Raises :class:`ConnectError` when the request was never sent, :class:`RequestTimeout`
        or :class:`TransportError` when it may have reached Salesforce.
        """
        try:
            r = await self.http.post(
                url, data=form, headers={"Accept": "application/json", "User-Agent": self.user_agent}
            )
        except _NEVER_SENT as exc:
            raise ConnectError(f"could not connect: {exc}", operation="oauth", method="POST") from exc
        except httpx.TimeoutException as exc:
            raise RequestTimeout(
                f"timed out: {type(exc).__name__}", operation="oauth", method="POST"
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"transport error: {exc!r}", operation="oauth", method="POST") from exc
        return RawResponse(r.status_code, r.headers, r.content, str(r.url), loads=self.loads)

    async def aclose(self) -> None:
        if self.poller is not None:
            await self.poller.stop()
            self.poller = None
        if not self.closed:
            self.closed = True
            if self.owns_http:
                await self.http.aclose()

    # --- pipeline --------------------------------------------------------------------

    async def send(
        self,
        spec: RequestSpec,
        runtime: AsyncAuthRuntime,
        *,
        caller: str | None,
        priority: str,
    ) -> RawResponse:
        """Run ``spec`` through the full pipeline and return the successful response."""
        if self.closed:
            raise TransportError(
                "client is closed", operation=spec.operation, method=spec.method, retryable=False
            )
        started = time.perf_counter()
        attempts = 0
        reasons: list[str] = []
        refreshed = False
        raw: RawResponse | None = None
        failure: BaseException | None = None
        bytes_out = len(spec.content) if spec.content else 0
        bytes_in = 0
        # A refresh failure this call may have to share must stay until the call ends.
        pinned_key = runtime.key  # sf.auth may rebind runtime.key while this call runs
        self.refresh_outcomes.pin(pinned_key)
        try:
            self.usage.check(priority, operation=spec.operation)
            token: Token | None = None
            retries_used = 0
            while True:
                # The gate is held for one attempt (and a refresh), never across a backoff
                # sleep, so a retrying call doesn't block unrelated calls.
                async with self.gate if self.gate is not None else _compat.nullcontext():
                    if token is None:
                        token = await runtime.token()
                    attempts += 1
                    sent = True
                    generation = runtime.generation()
                    try:
                        raw, wire_in, wire_out = await self._attempt(spec, token, runtime.instance_url)
                        bytes_in += wire_in
                        bytes_out = wire_out
                        err = classify(raw, spec, self.error_tags)
                    except _AttemptFailed as f:
                        raw, err, sent = None, f.error, f.sent
                    if err is None:
                        assert raw is not None
                        if spec.expect_json and raw.content and raw.status != 304:
                            raw.json()  # raises InvalidJSONResponse inside the pipeline
                        return raw

                    # SOAP reports a rejected session as an HTTP 500 fault with INVALID_SESSION_ID.
                    if isinstance(err, SessionExpired) and (
                        err.status == 401 or err.error_code == "INVALID_SESSION_ID"
                    ):
                        if refreshed or not runtime.can_refresh or not resend_after_401(spec, err):
                            if refreshed:
                                err.message = f"{err.message} (still rejected after one token refresh)"
                                err.args = (err.message,)
                            raise err
                        token = await runtime.refresh(token, entered=generation)
                        refreshed = True
                        continue

                    decision = decide_retry(spec, err, sent=sent)
                    if not decision.retry or retries_used + 1 >= self.retry.max_attempts:
                        raise err
                    if decision.delay is not None and self.retry.respect_retry_after:
                        if decision.delay > self.retry.max_retry_after:
                            raise err
                        delay = decision.delay
                    else:
                        delay = backoff_delay(self.retry, retries_used + 1, self.rng)
                    retries_used += 1
                    reasons.append(decision.reason or "retry")
                    self.emit(
                        "sf.retry",
                        {
                            "operation": spec.operation,
                            "attempt": attempts,
                            "reason": decision.reason,
                            "delay_ms": round(delay * 1000, 1),
                            "error_code": err.error_code,
                            "status": err.status,
                        },
                    )
                await self.sleep(delay)
        except BaseException as exc:
            # Not only SalesforceError: a rejected path (ValueError), a cancelled task or a
            # bug must not be reported as outcome="ok".
            failure = exc
            raise
        finally:
            self.refresh_outcomes.unpin(pinned_key)
            self._finish(
                spec,
                runtime,
                raw,
                failure,
                started,
                attempts,
                reasons,
                refreshed,
                bytes_in,
                bytes_out,
                caller,
                priority,
            )

    async def _attempt(
        self, spec: RequestSpec, token: Token, instance_url: str | None = None
    ) -> tuple[RawResponse, int, int]:
        base = instance_url or token.instance_url
        url = build_url(base, self.api_version, spec.path)
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token.access_token}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": self.user_agent,
        }
        content = spec.content
        if spec.session_body is not None:
            # SOAP: the token travels in SessionHeader inside the body, built per attempt.
            del headers["Authorization"]
            content = spec.session_body(token.access_token)
        headers.update(spec.headers)
        if self.call_options:
            headers = merge_call_options(headers, self.call_options)
        if content is not None:
            headers.setdefault("Content-Type", spec.content_type or "application/json")
            if spec.compress and len(content) > GZIP_THRESHOLD:
                content = gzip_body(content)
                headers["Content-Encoding"] = "gzip"
        request = self.http.build_request(
            spec.method,
            url,
            params=spec.params,
            content=content,
            headers=headers,  # pyright: ignore[reportArgumentType]
        )
        try:
            response = await self.http.send(request)
        except _NEVER_SENT as exc:
            raise _AttemptFailed(
                ConnectError(f"could not connect: {exc}", operation=spec.operation, method=spec.method),
                sent=False,
            ) from exc
        except httpx.TimeoutException as exc:
            raise _AttemptFailed(
                RequestTimeout(
                    f"timed out: {type(exc).__name__}", operation=spec.operation, method=spec.method
                ),
                sent=True,
            ) from exc
        except (httpx.TransportError, httpx.RequestError) as exc:
            # RequestError also covers DecodingError (bad gzip body) and TooManyRedirects.
            raise _AttemptFailed(
                TransportError(f"transport error: {exc!r}", operation=spec.operation, method=spec.method),
                sent=True,
            ) from exc
        info = parse_limit_info(response.headers.get("sforce-limit-info"))
        if info is not None:
            self.usage.observe(info)
        if self.debug >= 2:
            self.emit(
                "sf.debug.attempt",
                {
                    "method": spec.method,
                    # path template, not the URL: paths can carry external id values
                    "path_template": spec.path_template or path_template(spec.path),
                    "status": response.status_code,
                    "request_headers": {
                        k: ("<redacted>" if k.lower() in _REDACT_HEADERS else v) for k, v in headers.items()
                    },
                    "response_headers": {
                        k: ("<redacted>" if k.lower() in _REDACT_HEADERS else v)
                        for k, v in response.headers.items()
                    },
                    "elapsed_ms": round(response.elapsed.total_seconds() * 1000, 1),
                },
            )
        raw = RawResponse(response.status_code, response.headers, response.content, url, loads=self.loads)
        return raw, response.num_bytes_downloaded, len(content) if content else 0

    def _error_message(self, spec: RequestSpec, error: SalesforceError | None) -> str | None:
        # Salesforce DML messages can quote record values (e.g. STRING_TOO_LONG), and §11.3 says
        # record bodies are never logged: the message is only added for ESF_DEBUG, which the
        # developer turns on explicitly. The raised exception always keeps the full message.
        if error is None or not error.message or not self.debug:
            return None
        message = error.message
        if spec.soql:
            # MALFORMED_QUERY / INVALID_FIELD messages echo the query text, literals included.
            message = redact_soql(message)
        return message[:200]

    def _finish(
        self,
        spec: RequestSpec,
        runtime: AsyncAuthRuntime,
        raw: RawResponse | None,
        failure: BaseException | None,
        started: float,
        attempts: int,
        reasons: list[str],
        refreshed: bool,
        bytes_in: int,
        bytes_out: int,
        caller: str | None,
        priority: str,
    ) -> None:
        used, mx = self.usage.latest()
        error = failure if isinstance(failure, SalesforceError) else None
        records = spec.records
        records_failed: int | None = None
        if failure is None and raw is not None and raw.is_json:
            try:
                body = raw.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and isinstance(body.get("records"), list):  # pyright: ignore[reportUnknownMemberType]
                records = len(body["records"])  # pyright: ignore[reportUnknownArgumentType]
            elif isinstance(body, list):
                items = cast("list[Any]", body)
                if spec.records is None:
                    records = len(items)
                if spec.operation.startswith("collections."):
                    records_failed = sum(1 for x in items if _failed_row(x))
        fields: dict[str, Any] = {
            "operation": spec.operation,
            "sobject": spec.sobject,
            "method": spec.method,
            "path_template": spec.path_template or path_template(spec.path),
            "status": raw.status if raw is not None else (error.status if error else None),
            "outcome": "error" if failure is not None else "ok",
            "error_code": error.error_code if error else None,
            "error_class": type(failure).__name__ if failure is not None else None,
            "error_message": self._error_message(spec, error),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "attempts": attempts,
            "retry_reasons": reasons,
            "refreshed": refreshed,
            "records": records,
            "records_failed": records_failed,
            "bytes_in": bytes_in,
            "bytes_out": bytes_out,
            "api_used": used,
            "api_max": mx,
            "caller": caller,
            "priority": priority,
            "soql_hash": soql_hash(spec.soql) if spec.soql else None,
            "request_id": (raw.request_id if raw is not None else (error.request_id if error else None)),
            "api_version": self.api_version,
            "client_id_hash": runtime.auth.client_id_hash or None,
        }
        if spec.soql and self.log_soql == "redacted":
            fields["soql"] = redact_soql(spec.soql)
        error_label: str | None = None
        if failure is not None:
            error_label = (error.error_code if error else None) or type(failure).__name__
        self.usage.record_call(
            operation=spec.operation,
            sobject=spec.sobject,
            caller=caller,
            error_code=error_label,
            retries=len(reasons),
        )
        self.emit("sf.request", fields)
