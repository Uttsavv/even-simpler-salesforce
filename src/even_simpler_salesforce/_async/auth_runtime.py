"""Auth runtime: in-memory tokens, single-flight refresh and token-store interaction (§6.4)."""

from __future__ import annotations

import copy
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..auth.flows import Auth
from ..auth.store import AsyncTokenStore, store_lock_async
from ..auth.token import Token, merge_token, parse_token_response
from ..core.http import RawResponse
from ..core.retry import backoff_delay
from ..errors import (
    ConnectError,
    OAuthError,
    ReauthorizationRequired,
    RefreshFailed,
    SalesforceError,
    SessionExpired,
    TokenConflict,
    TransportError,
    UnexpectedResponse,
)

if TYPE_CHECKING:
    from .transport import AsyncTransport

__all__ = ["AsyncAuthRuntime"]

_log = logging.getLogger("esf")

EXPIRY_SKEW = timedelta(seconds=60)
BUSY_ATTEMPTS = 3  # total token POSTs while Salesforce says "already being processed"
_BUSY_TEXT = "already being processed"


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class AsyncAuthRuntime:
    """Holds the current token for one ``Auth`` and refreshes it.

    The in-memory token map and the per-key locks live on the shared transport, so every client
    in a family that uses the same ``token_key`` shares one token and one in-flight refresh.
    """

    def __init__(self, auth: Auth, transport: AsyncTransport, store: AsyncTokenStore) -> None:
        self.auth = auth
        self.transport = transport
        self.store = store
        self.key = auth.token_key
        self.key_hash = key_hash(self.key)
        self._introspect_warned = False

    # --- public ----------------------------------------------------------------------

    @property
    def can_refresh(self) -> bool:
        return self.auth.kind != "static"

    def current(self) -> Token | None:
        """The in-memory token, without any I/O."""
        return self.transport.tokens.get(self.key)

    async def token(self) -> Token:
        """A token to send, acquiring or proactively refreshing it if needed."""
        tok = self.transport.tokens.get(self.key)
        if tok is None:
            tok = await self._load_or_acquire()
        if self._due(tok):
            tok = await self.refresh(tok, proactive=True)
        return tok

    def generation(self) -> int:
        """Refresh generation for this key; capture it before sending a request and pass it to
        :meth:`refresh` so a refresh that failed while the request was in flight is shared."""
        return self._generation()

    async def refresh(self, stale: Token, *, proactive: bool = False, entered: int | None = None) -> Token:
        """Refresh after ``stale`` was rejected (or is due). Concurrent callers share one refresh.

        Args:
            stale: The token that was rejected.
            proactive: The refresh is ahead of expiry (for events).
            entered: :meth:`generation` captured before the rejected request was sent.
        """
        if not self.can_refresh:
            raise SessionExpired(
                "access token expired or invalid and StaticToken can't be refreshed",
                error_code="INVALID_SESSION_ID",
                status=401,
            )
        if entered is None:
            entered = self._generation()
        async with self.transport.lock_for(self.key):
            cur = self.transport.tokens.get(self.key)
            if cur is not None and cur.access_token != stale.access_token and not self._due(cur):
                return cur  # another caller already refreshed
            self._raise_shared_failure(entered)
            new = await self._refresh_recorded(stale, proactive=proactive)
            self.transport.tokens[self.key] = new
            return new

    async def set_token(self, token: Token) -> Token:
        """Save ``token`` to the store and memory (used by ``exchange_code``)."""
        async with self.transport.lock_for(self.key):
            current = await self.store.load(self.key)
            saved = await self.store.save(
                self.key, token, expected_version=current.version if current is not None else None
            )
            self.transport.tokens[self.key] = saved
            return saved

    async def forget(self) -> None:
        async with self.transport.lock_for(self.key):
            self.transport.tokens.pop(self.key, None)
            await self.store.delete(self.key)

    # --- internals -------------------------------------------------------------------

    def _due(self, tok: Token) -> bool:
        now = self.transport.now()
        ra = self.auth.refresh_ahead
        if ra is not None and tok.issued_at + ra <= now:
            return True
        return tok.expires_at is not None and tok.expires_at <= now + EXPIRY_SKEW

    async def _load_or_acquire(self) -> Token:
        static = self.auth.static_token()
        if static is not None:
            self.transport.tokens.setdefault(self.key, static)
            return self.transport.tokens[self.key]
        entered = self._generation()
        async with self.transport.lock_for(self.key):
            tok = self.transport.tokens.get(self.key)
            if tok is not None:
                return tok
            tok = await self.store.load(self.key)
            if tok is None:
                self._raise_shared_failure(entered)
                tok = await self._refresh_recorded(None, proactive=False)
            self.transport.tokens[self.key] = tok
            return tok

    # Concurrent callers queue on the per-key lock while one of them refreshes. If that refresh
    # fails, the waiters must not each POST to the token endpoint again (a stampede that can trip
    # login rate limits): every caller that was already waiting gets a copy of the same error.
    # A call that starts after the failure was recorded makes a fresh attempt.

    def _generation(self) -> int:
        return self.transport.refresh_outcomes.get(self.key, (0, None))[0]

    def _raise_shared_failure(self, entered: int) -> None:
        generation, failure = self.transport.refresh_outcomes.get(self.key, (0, None))
        if generation != entered and failure is not None:
            shared = copy.copy(failure)
            shared.__cause__ = failure.__cause__
            raise shared

    async def _refresh_recorded(self, stale: Token | None, *, proactive: bool) -> Token:
        generation = self._generation() + 1
        try:
            new = await self._refresh_locked(stale, proactive=proactive)
        except SalesforceError as exc:
            kept = copy.copy(exc)  # no traceback: don't keep the failing frames alive
            kept.__cause__ = exc.__cause__
            self.transport.refresh_outcomes[self.key] = (generation, kept)
            raise
        self.transport.refresh_outcomes[self.key] = (generation, None)
        return new

    async def _post_form(self, url: str, form: dict[str, str]) -> RawResponse:
        return await self.transport.post_form(url, form)

    async def _reload_if_changed(self, base: Token | None) -> Token | None:
        reloaded = await self.store.load(self.key)
        if reloaded is not None and (base is None or reloaded.access_token != base.access_token):
            return reloaded
        return None

    async def _refresh_locked(self, stale: Token | None, *, proactive: bool) -> Token:
        started = time.perf_counter()
        policy = self.transport.retry
        async with store_lock_async(self.store, self.key):
            current = await self.store.load(self.key)
            newer = current is not None and (stale is None or current.access_token != stale.access_token)
            if newer and current is not None and not self._due(current):
                return current  # another process refreshed already
            base = current or stale
            attempts = 0
            busy = 0
            while True:
                attempts += 1
                form = self.auth.token_form(base)  # may raise ReauthorizationRequired
                try:
                    resp = await self._post_form(self.auth.token_url, form)
                except ConnectError as exc:  # never sent: safe to retry
                    if attempts < policy.max_attempts:
                        await self.transport.sleep(backoff_delay(policy, attempts))
                        continue
                    raise RefreshFailed(f"token endpoint unreachable: {exc}", ambiguous=False) from exc
                except TransportError as exc:  # may have reached Salesforce
                    if not self.auth.uses_refresh_token:
                        if attempts < policy.max_attempts:  # a fresh token request is safe to resend
                            await self.transport.sleep(backoff_delay(policy, attempts))
                            continue
                        raise RefreshFailed(f"token request failed: {exc}", ambiguous=True) from exc
                    # The refresh may have reached Salesforce (and rotated the token): never resend.
                    await self.transport.sleep(policy.base_delay)
                    reloaded = await self._reload_if_changed(base)
                    if reloaded is not None:
                        return reloaded
                    raise RefreshFailed(
                        "token refresh outcome unknown (timeout/reset after send); not resent",
                        ambiguous=True,
                    ) from exc

                if resp.status == 200:
                    break

                err, desc = self._oauth_error(resp)
                if _BUSY_TEXT in desc.lower() or _BUSY_TEXT in err.lower():
                    busy += 1
                    if busy >= BUSY_ATTEMPTS:  # BUSY_ATTEMPTS POSTs in total
                        raise RefreshFailed(desc or err, error_code=err or None, status=resp.status)
                    await self.transport.sleep(backoff_delay(policy, busy))
                    reloaded = await self._reload_if_changed(base)
                    if reloaded is not None:
                        return reloaded
                    continue
                if err == "invalid_grant":
                    self.transport.emit("sf.auth.reauth_required", {"token_key_hash": self.key_hash})
                    raise ReauthorizationRequired(
                        desc or "refresh token expired, revoked or rotated out",
                        error_code=err,
                        status=resp.status,
                        operation="oauth.refresh",
                    )
                if resp.status >= 500 and not self.auth.uses_refresh_token and attempts < policy.max_attempts:
                    await self.transport.sleep(backoff_delay(policy, attempts))
                    continue
                if err:
                    raise OAuthError(
                        desc or err,
                        error=err,
                        error_description=desc or None,
                        status=resp.status,
                        operation="oauth.refresh",
                    )
                raise RefreshFailed(
                    f"token endpoint returned HTTP {resp.status}",
                    status=resp.status,
                    operation="oauth.refresh",
                    raw=resp.content[:4096],
                )

            try:
                data = resp.json()
            except ValueError:
                raise UnexpectedResponse(
                    "token endpoint returned non-JSON", raw=resp.content[:4096]
                ) from None
            if not isinstance(data, dict):
                raise UnexpectedResponse("token endpoint returned unexpected JSON")
            new = merge_token(parse_token_response(data, now=self.transport.now()), base)  # pyright: ignore[reportUnknownArgumentType]
            rotated = bool(
                base is not None
                and base.refresh_token
                and new.refresh_token
                and new.refresh_token != base.refresh_token
            )
            # Save (incl. a rotated refresh token) BEFORE the new access token is used anywhere.
            try:
                saved = await self.store.save(
                    self.key, new, expected_version=current.version if current is not None else None
                )
            except TokenConflict:
                reloaded = await self.store.load(self.key)
                if reloaded is None:
                    raise
                return reloaded
            if self.auth.expiry == "introspect":
                saved = await self._introspect_and_save(saved)

        if stale is not None:
            self.transport.usage.record_refresh()
        self.transport.emit(
            "sf.auth.refreshed",
            {
                "token_key_hash": self.key_hash,
                "flow": self.auth.kind,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "rotated": rotated,
                "proactive": proactive,
                "initial": stale is None,
            },
        )
        return saved

    @staticmethod
    def _oauth_error(resp: RawResponse) -> tuple[str, str]:
        try:
            body: Any = resp.json()
        except ValueError:
            return "", ""
        if isinstance(body, dict):
            return str(body.get("error") or ""), str(body.get("error_description") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        return "", ""

    async def introspect(self, token: Token, *, hint: str = "access_token") -> dict[str, Any]:
        """``POST /services/oauth2/introspect`` for ``token``."""
        value = token.access_token if hint == "access_token" else (token.refresh_token or "")
        form = {"token": value, "token_type_hint": hint, **self.auth.client_auth_form()}
        resp = await self._post_form(self.auth.base_url + "/services/oauth2/introspect", form)
        if resp.status != 200:
            err, desc = self._oauth_error(resp)
            raise OAuthError(
                desc or err or f"HTTP {resp.status}",
                error=err or None,
                error_description=desc or None,
                status=resp.status,
                operation="oauth.introspect",
            )
        data: Any = resp.json()
        return data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]

    async def _introspect_and_save(self, saved: Token) -> Token:
        try:
            info = await self.introspect(saved)
        except Exception as exc:
            self._warn_introspect(f"introspection failed ({type(exc).__name__}); using 401-driven refresh")
            return saved
        exp = info.get("exp")
        if not isinstance(exp, (int, float)):
            self._warn_introspect("introspection returned no 'exp'; using 401-driven refresh")
            return saved
        updated = saved.replace(expires_at=datetime.fromtimestamp(float(exp), tz=timezone.utc))
        try:
            return await self.store.save(self.key, updated, expected_version=saved.version)
        except TokenConflict:
            return (await self.store.load(self.key)) or saved

    def _warn_introspect(self, msg: str) -> None:
        if not self._introspect_warned:
            self._introspect_warned = True
            _log.info("esf: %s", msg)
