"""OAuth flow configuration classes (§6.1). Frozen config objects; they hold no tokens.

Each flow knows how to build the form for acquiring/refreshing a token. The I/O lives in the
client's auth runtime.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Literal
from urllib.parse import urlencode, urlparse

from ..errors import ReauthorizationRequired
from .token import Token

__all__ = [
    "Auth",
    "AuthStart",
    "ClientCredentials",
    "JwtBearer",
    "StaticToken",
    "WebServerFlow",
    "resolve_domain",
]

_HOSTS = {"login": "https://login.salesforce.com", "test": "https://test.salesforce.com"}

Expiry = Literal["unknown", "introspect"]


def resolve_domain(domain: str) -> str:
    """``"login"`` / ``"test"`` / ``https://<mydomain>.my.salesforce.com`` → base URL."""
    if domain in _HOSTS:
        return _HOSTS[domain]
    parsed = urlparse(domain)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError(f"domain must be 'login', 'test' or an https:// origin, got {domain!r}")
    return f"https://{parsed.netloc}"


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class AuthStart:
    """Result of :meth:`WebServerFlow.authorization_url`. Keep ``state`` and ``code_verifier``
    in the user's session until the callback."""

    url: str
    state: str
    code_verifier: str | None


class Auth:
    """Base class for login flows.

    Subclasses are frozen dataclasses. The runtime only uses the members defined here.
    """

    kind: ClassVar[str] = "custom"
    #: The refresh uses a (possibly rotating) refresh token: never resend blindly.
    uses_refresh_token: ClassVar[bool] = False

    domain: str
    client_id: str
    client_secret: str | None
    refresh_ahead: timedelta | None
    expiry: Expiry

    @property
    def token_key(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def base_url(self) -> str:
        return resolve_domain(self.domain)

    @property
    def token_url(self) -> str:
        return self.base_url + "/services/oauth2/token"

    @property
    def client_id_hash(self) -> str:
        return _short_hash(self.client_id)

    def static_token(self) -> Token | None:
        """A token known without any request (``StaticToken``)."""
        return None

    def token_form(self, current: Token | None) -> dict[str, str]:
        """Form for acquiring a new token, given the current one (may be ``None``)."""
        raise NotImplementedError  # pragma: no cover

    def client_auth_form(self) -> dict[str, str]:
        form = {"client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        return form


@dataclass(frozen=True, slots=True)
class WebServerFlow(Auth):
    """Authorization-code flow with PKCE, for users who log in through the browser.

    The app holds the user's refresh token in a :class:`TokenStore` under ``token_key``.
    """

    kind: ClassVar[str] = "web_server"
    uses_refresh_token: ClassVar[bool] = True

    domain: str
    client_id: str
    redirect_uri: str
    client_secret: str | None = field(default=None, repr=False)
    scopes: Sequence[str] = ("api", "refresh_token")
    key: str | None = None
    refresh_ahead: timedelta | None = None
    expiry: Expiry = "unknown"
    pkce: bool = True

    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        redirect_uri: str,
        client_secret: str | None = None,
        scopes: Sequence[str] = ("api", "refresh_token"),
        token_key: str | None = None,
        refresh_ahead: timedelta | None = None,
        expiry: Expiry = "unknown",
        pkce: bool = True,
    ) -> None:
        resolve_domain(domain)
        set_ = object.__setattr__
        set_(self, "domain", domain)
        set_(self, "client_id", client_id)
        set_(self, "redirect_uri", redirect_uri)
        set_(self, "client_secret", client_secret)
        set_(self, "scopes", tuple(scopes))
        set_(self, "key", token_key)
        set_(self, "refresh_ahead", refresh_ahead)
        set_(self, "expiry", expiry)
        set_(self, "pkce", pkce)

    @property
    def token_key(self) -> str:
        return self.key or f"web_server:{_short_hash(self.base_url + '|' + self.client_id)}"

    def authorization_url(
        self,
        state: str | None = None,
        *,
        scopes: Sequence[str] | None = None,
        prompt: str | None = None,
        login_hint: str | None = None,
    ) -> AuthStart:
        """Build the URL to redirect the user to. Pure; no I/O."""
        state = state or secrets.token_urlsafe(24)
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        sc = tuple(scopes) if scopes is not None else tuple(self.scopes)
        if sc:
            params["scope"] = " ".join(sc)
        if prompt:
            params["prompt"] = prompt
        if login_hint:
            params["login_hint"] = login_hint
        verifier: str | None = None
        if self.pkce:
            verifier = secrets.token_urlsafe(64)
            params["code_challenge"] = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
            params["code_challenge_method"] = "S256"
        return AuthStart(
            url=f"{self.base_url}/services/oauth2/authorize?{urlencode(params)}",
            state=state,
            code_verifier=verifier,
        )

    def code_form(self, code: str, code_verifier: str | None) -> dict[str, str]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            **self.client_auth_form(),
        }
        if code_verifier:
            form["code_verifier"] = code_verifier
        return form

    def token_form(self, current: Token | None) -> dict[str, str]:
        if current is None or not current.refresh_token:
            raise ReauthorizationRequired(
                f"no refresh token stored for {self.token_key!r}; complete the authorization flow",
                error_code="no_refresh_token",
            )
        return {
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
            **self.client_auth_form(),
        }


@dataclass(frozen=True, slots=True)
class ClientCredentials(Auth):
    """Client-credentials flow: the app acts as its integration user.

    ``domain`` must be the org's My Domain URL; ``login``/``test`` aren't supported by
    Salesforce for this flow. No refresh token is issued; "refresh" requests a new token.
    """

    kind: ClassVar[str] = "client_credentials"

    domain: str
    client_id: str
    client_secret: str | None = field(default=None, repr=False)
    key: str | None = None
    refresh_ahead: timedelta | None = None
    expiry: Expiry = "unknown"

    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        client_secret: str,
        token_key: str | None = None,
        refresh_ahead: timedelta | None = None,
        expiry: Expiry = "unknown",
    ) -> None:
        if domain in _HOSTS:
            raise ValueError(
                "ClientCredentials needs the org's My Domain URL "
                "(https://<mydomain>.my.salesforce.com); login/test aren't supported"
            )
        resolve_domain(domain)
        set_ = object.__setattr__
        set_(self, "domain", domain)
        set_(self, "client_id", client_id)
        set_(self, "client_secret", client_secret)
        set_(self, "key", token_key)
        set_(self, "refresh_ahead", refresh_ahead)
        set_(self, "expiry", expiry)

    @property
    def token_key(self) -> str:
        return self.key or f"client_credentials:{_short_hash(self.base_url + '|' + self.client_id)}"

    def token_form(self, current: Token | None) -> dict[str, str]:
        return {"grant_type": "client_credentials", **self.client_auth_form()}


@dataclass(frozen=True, slots=True)
class JwtBearer(Auth):
    """JWT bearer flow: server-to-server with a certificate. Needs the ``[jwt]`` extra."""

    kind: ClassVar[str] = "jwt_bearer"

    domain: str
    client_id: str
    username: str
    private_key: str | bytes = field(repr=False)
    audience: str | None = None
    key: str | None = None
    refresh_ahead: timedelta | None = None
    expiry: Expiry = "unknown"
    client_secret: str | None = field(default=None, repr=False)
    assertion_lifetime: int = 180

    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        username: str,
        private_key: str | bytes,
        audience: str | None = None,
        token_key: str | None = None,
        refresh_ahead: timedelta | None = None,
        expiry: Expiry = "unknown",
    ) -> None:
        resolve_domain(domain)
        set_ = object.__setattr__
        set_(self, "domain", domain)
        set_(self, "client_id", client_id)
        set_(self, "username", username)
        set_(self, "private_key", private_key)
        set_(self, "audience", audience)
        set_(self, "key", token_key)
        set_(self, "refresh_ahead", refresh_ahead)
        set_(self, "expiry", expiry)
        set_(self, "client_secret", None)
        set_(self, "assertion_lifetime", 180)

    @property
    def token_key(self) -> str:
        return self.key or f"jwt_bearer:{_short_hash(self.base_url + '|' + self.client_id + '|' + self.username)}"

    @property
    def aud(self) -> str:
        """``aud`` claim: ``login``/``test``/a custom URL; inferred from ``domain`` when unset."""
        if self.audience is None:
            sandbox = self.domain == "test" or ".sandbox.my.salesforce.com" in self.domain
            return _HOSTS["test" if sandbox else "login"]
        return _HOSTS.get(self.audience, self.audience)

    def assertion(self, now: float | None = None) -> str:
        try:
            import jwt  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ImportError(
                "JwtBearer needs the [jwt] extra: pip install 'even-simpler-salesforce[jwt]'"
            ) from exc
        issued = int(now if now is not None else time.time())
        claims: dict[str, Any] = {
            "iss": self.client_id,
            "sub": self.username,
            "aud": self.aud,
            "exp": issued + self.assertion_lifetime,
        }
        token: Any = jwt.encode(claims, self.private_key, algorithm="RS256")  # pyright: ignore
        return token if isinstance(token, str) else token.decode()

    def token_form(self, current: Token | None) -> dict[str, str]:
        return {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": self.assertion(),
        }


@dataclass(frozen=True, slots=True)
class StaticToken(Auth):
    """A token obtained elsewhere. No refresh: a 401 raises :class:`SessionExpired`."""

    kind: ClassVar[str] = "static"

    access_token: str = field(repr=False)
    instance_url: str
    domain: str = "login"
    client_id: str = ""
    client_secret: str | None = None
    refresh_ahead: timedelta | None = None
    expiry: Expiry = "unknown"

    def __init__(self, access_token: str, instance_url: str) -> None:
        set_ = object.__setattr__
        set_(self, "access_token", access_token)
        set_(self, "instance_url", instance_url.rstrip("/"))
        set_(self, "domain", "login")
        set_(self, "client_id", "")
        set_(self, "client_secret", None)
        set_(self, "refresh_ahead", None)
        set_(self, "expiry", "unknown")

    @property
    def token_key(self) -> str:
        return f"static:{_short_hash(self.instance_url + '|' + self.access_token)}"

    @property
    def client_id_hash(self) -> str:
        return ""

    def static_token(self) -> Token | None:
        return Token(
            access_token=self.access_token,
            instance_url=self.instance_url,
            issued_at=datetime.now(timezone.utc),
        )

    def token_form(self, current: Token | None) -> dict[str, str]:  # pragma: no cover
        raise NotImplementedError("StaticToken can't be refreshed")
