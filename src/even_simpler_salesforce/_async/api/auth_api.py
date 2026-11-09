"""OAuth helpers on the client: ``sf.auth`` (§6.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...auth.flows import WebServerFlow
from ...auth.token import Token, parse_token_response
from ...core.http import RequestSpec
from ...errors import OAuthError, UnexpectedResponse

if TYPE_CHECKING:
    from ..auth_runtime import AsyncAuthRuntime
    from ..client import AsyncSalesforce

__all__ = ["AsyncAuthApi"]


class AsyncAuthApi:
    """``sf.auth``: code exchange, revoke, userinfo and introspection."""

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client

    def _runtime_for(self, token_key: str | None) -> AsyncAuthRuntime:
        rt = self._sf._runtime
        if token_key is None or token_key == rt.key:
            return rt
        from ..auth_runtime import AsyncAuthRuntime as _RT

        clone = _RT(rt.auth, rt.transport, rt.store)
        clone.key = token_key
        return clone

    async def exchange_code(
        self, flow: WebServerFlow, code: str, *, code_verifier: str | None = None
    ) -> Token:
        """Exchange an authorization code for tokens and save them under ``flow.token_key``."""
        from ..auth_runtime import AsyncAuthRuntime as _RT

        rt = _RT(flow, self._sf._transport, self._sf._runtime.store)
        resp = await rt._post_form(flow.token_url, flow.code_form(code, code_verifier))
        if resp.status != 200:
            err, desc = rt._oauth_error(resp)
            raise OAuthError(
                desc or err or f"HTTP {resp.status}",
                error=err or None,
                error_description=desc or None,
                status=resp.status,
                operation="oauth.exchange_code",
            )
        try:
            data: Any = resp.json()
        except ValueError:
            raise UnexpectedResponse("token endpoint returned non-JSON") from None
        token = parse_token_response(data, now=self._sf._transport.now())
        saved = await rt.set_token(token)
        if flow.expiry == "introspect":
            saved = await rt._introspect_and_save(saved)
            self._sf._transport.tokens[rt.key] = saved
        return saved

    async def revoke(self, token_key: str | None = None) -> None:
        """``POST /services/oauth2/revoke`` and remove the token from memory and the store."""
        rt = self._runtime_for(token_key)
        tok = rt.transport.tokens.get(rt.key) or await rt.store.load(rt.key)
        if tok is not None and rt.auth.kind != "static":
            value = tok.refresh_token or tok.access_token
            resp = await rt._post_form(rt.auth.base_url + "/services/oauth2/revoke", {"token": value})
            if resp.status not in (200, 204):
                err, desc = rt._oauth_error(resp)
                # An already-invalid token is as good as revoked.
                if err not in ("invalid_token", "unsupported_token_type"):
                    raise OAuthError(
                        desc or err or f"HTTP {resp.status}",
                        error=err or None,
                        error_description=desc or None,
                        status=resp.status,
                        operation="oauth.revoke",
                    )
        await rt.forget()

    async def userinfo(self) -> dict[str, Any]:
        """``GET /services/oauth2/userinfo`` for the current token."""
        raw = await self._sf._send(
            RequestSpec("GET", "/services/oauth2/userinfo", idempotent=True, operation="oauth.userinfo")
        )
        return raw.json()

    async def introspect(self, token_key: str | None = None) -> dict[str, Any]:
        """``POST /services/oauth2/introspect`` for the access token under ``token_key``."""
        rt = self._runtime_for(token_key)
        tok = rt.transport.tokens.get(rt.key) or await rt.store.load(rt.key)
        if tok is None:
            tok = await rt.token()
        return await rt.introspect(tok)

    async def token(self) -> Token:
        """The current token (acquiring one if needed). Treat it as a secret."""
        return await self._sf._runtime.token()
