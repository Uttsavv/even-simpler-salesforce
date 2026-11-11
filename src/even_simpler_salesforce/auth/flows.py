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
from urllib.parse import urlencode, urlsplit

from ..core.http import check_instance_url
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


_SEGMENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")


def resolve_domain(domain: str) -> str:
    """Resolve a login flow's ``domain`` to the base URL of its OAuth endpoints.

    Accepted values:

    - ``"login"`` or ``"test"``;
    - an ``https://`` origin, such as ``https://acme.my.salesforce.com``;
    - an ``https://`` Experience Cloud site URL with a path prefix, such as
      ``https://acme.my.site.com/partners``. Salesforce serves the site's OAuth endpoints
      under that prefix (``<site URL>/services/oauth2/token``).

    A trailing ``/`` is dropped. Paths may hold only unreserved URL characters, with no
    empty, ``.`` or ``..`` segments.

    See https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_endpoints.htm&type=5
    (OAuth endpoints on a My Domain or Experience Cloud site URL).

    Raises:
        ValueError: anything else, including ``http://`` URLs, bare host names, user info,
            query strings and fragments.
    """
    if domain in _HOSTS:
        return _HOSTS[domain]
    error = (
        f"domain must be 'login', 'test', an https:// origin or an Experience Cloud site URL, got {domain!r}"
    )
    parsed = urlsplit(domain)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or "@" in parsed.netloc
        or parsed.query
        or parsed.fragment
        or "?" in domain
        or "#" in domain
    ):
        raise ValueError(error)
    path = parsed.path.rstrip("/")
    if path:
        segments = path.split("/")[1:]
        if any(seg in ("", ".", "..") or not set(seg) <= _SEGMENT_CHARS for seg in segments):
            raise ValueError(error)
    return f"https://{parsed.netloc}{path}"


def _site_path(domain: str) -> str:
    """The path prefix of an Experience Cloud site ``domain`` (``""`` for an origin)."""
    return urlsplit(resolve_domain(domain)).path


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class AuthStart:
    """Result of :meth:`WebServerFlow.authorization_url`. Keep ``state`` and ``code_verifier``
    in the user's session until the callback."""

    url: str
    state: str = field(repr=False)
    #: PKCE secret: ``repr()`` leaves it out, like ``state``.
    code_verifier: str | None = field(repr=False)


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
        """Key of this flow's record in the token store."""
        raise NotImplementedError

    @property
    def base_url(self) -> str:
        """Base URL of the OAuth endpoints, resolved from ``domain`` (see :func:`resolve_domain`)."""
        return resolve_domain(self.domain)

    @property
    def token_url(self) -> str:
        """The OAuth token endpoint (``/services/oauth2/token``)."""
        return self.base_url + "/services/oauth2/token"

    @property
    def client_id_hash(self) -> str:
        """Short SHA-256 prefix of ``client_id``, used in events instead of the id."""
        return _short_hash(self.client_id)

    def static_token(self) -> Token | None:
        """A token known without any request (``StaticToken``)."""
        return None

    def token_form(self, current: Token | None) -> dict[str, str]:
        """Form for acquiring a new token, given the current one (may be ``None``)."""
        raise NotImplementedError  # pragma: no cover

    def client_auth_form(self) -> dict[str, str]:
        """``client_id`` (and ``client_secret`` when set) for OAuth form posts."""
        form = {"client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        return form


@dataclass(frozen=True, slots=True)
class WebServerFlow(Auth):
    """Authorization-code flow with PKCE, for users who log in through the browser.

    The app holds the user's refresh token in a :class:`TokenStore` under ``token_key``, which
    is required: one key per Salesforce identity (e.g. ``f"user:{user_id}"``).
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
        token_key: str,
        refresh_ahead: timedelta | None = None,
        expiry: Expiry = "unknown",
        pkce: bool = True,
    ) -> None:
        if not isinstance(token_key, str) or not token_key:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError(
                "WebServerFlow needs token_key= naming the Salesforce identity whose tokens it uses "
                "(e.g. f'user:{user_id}'); without it every user would share one token record"
            )
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
        """The ``token_key`` passed to the constructor."""
        return self.key or ""  # always set: required in __init__

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
        """Form for exchanging an authorization ``code`` (with the PKCE verifier, if any)."""
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
        """Refresh-token grant form.

        Raises:
            ReauthorizationRequired: no refresh token is stored.
        """
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
        if _site_path(domain):
            raise ValueError(
                "ClientCredentials needs the org's My Domain URL "
                "(https://<mydomain>.my.salesforce.com); Salesforce documents this flow on My "
                "Domain only, not on an Experience Cloud site path"
            )
        set_ = object.__setattr__
        set_(self, "domain", domain)
        set_(self, "client_id", client_id)
        set_(self, "client_secret", client_secret)
        set_(self, "key", token_key)
        set_(self, "refresh_ahead", refresh_ahead)
        set_(self, "expiry", expiry)

    @property
    def token_key(self) -> str:
        """``token_key`` if given, else a hash of the domain and client id."""
        return self.key or f"client_credentials:{_short_hash(self.base_url + '|' + self.client_id)}"

    def token_form(self, current: Token | None) -> dict[str, str]:
        """Client-credentials grant form."""
        return {"grant_type": "client_credentials", **self.client_auth_form()}


def _is_encrypted_pem(key: str | bytes) -> bool:
    text = key if isinstance(key, bytes) else key.encode("utf-8", "replace")
    # PKCS#8 "BEGIN ENCRYPTED PRIVATE KEY", or a traditional key with "Proc-Type: 4,ENCRYPTED".
    return b"-----BEGIN ENCRYPTED PRIVATE KEY-----" in text or b"Proc-Type: 4,ENCRYPTED" in text


def _signing_key(key: str | bytes, passphrase: str | bytes | None) -> Any:
    """The key to sign with: the PEM unchanged, or the decrypted key object.

    Errors never contain the key or the passphrase.
    """
    if passphrase is None:
        if _is_encrypted_pem(key):
            raise ValueError("private_key is an encrypted PEM key; pass private_key_passphrase")
        return key
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    data = key.encode() if isinstance(key, str) else key
    secret = passphrase.encode() if isinstance(passphrase, str) else passphrase
    try:
        return load_pem_private_key(data, password=secret)
    except TypeError:  # cryptography: a password was given but the key isn't encrypted
        raise ValueError(
            "private_key_passphrase was given, but private_key isn't an encrypted PEM key"
        ) from None
    except ValueError:  # wrong passphrase, or not a PEM private key
        raise ValueError(
            "could not decrypt private_key: wrong private_key_passphrase or not a PEM private key"
        ) from None


@dataclass(frozen=True, slots=True)
class JwtBearer(Auth):
    """JWT bearer flow: server-to-server with a certificate. Needs the ``[jwt]`` extra.

    Args:
        domain: Token endpoint host: ``"login"``, ``"test"`` or a My Domain URL.
        client_id: Consumer key, sent as the ``iss`` claim.
        username: The user to act as, sent as the ``sub`` claim.
        private_key: RSA private key in PEM format.
        private_key_passphrase: Passphrase of an encrypted PEM key. The client decrypts the key
            locally once, when you create the object; the passphrase is never sent, logged or
            shown by ``repr()``.
        audience: The ``aud`` claim; inferred from ``domain`` when unset.
        token_key: Token store key.
        refresh_ahead: Refresh proactively after this ``timedelta``.
        expiry: ``"introspect"`` reads the expiry from the introspection endpoint.

    Raises:
        ImportError: The ``[jwt]`` extra isn't installed.
        ValueError: The key is encrypted and ``private_key_passphrase`` is missing or wrong,
            or a passphrase was given for a key that isn't encrypted.
    """

    kind: ClassVar[str] = "jwt_bearer"

    domain: str
    client_id: str
    username: str
    private_key: str | bytes = field(repr=False)
    private_key_passphrase: str | bytes | None = field(default=None, repr=False)
    audience: str | None = None
    key: str | None = None
    refresh_ahead: timedelta | None = None
    expiry: Expiry = "unknown"
    client_secret: str | None = field(default=None, repr=False)
    assertion_lifetime: int = 180
    _signing_key: Any = field(default=None, repr=False, compare=False)

    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        username: str,
        private_key: str | bytes,
        private_key_passphrase: str | bytes | None = None,
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
        set_(self, "private_key_passphrase", private_key_passphrase)
        set_(self, "audience", audience)
        set_(self, "key", token_key)
        set_(self, "refresh_ahead", refresh_ahead)
        set_(self, "expiry", expiry)
        set_(self, "client_secret", None)
        set_(self, "assertion_lifetime", 180)
        try:  # fail at construction, not at the first call
            import jwt  # pyright: ignore[reportMissingImports, reportUnusedImport]  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise ImportError(
                "JwtBearer needs the [jwt] extra: pip install 'even-simpler-salesforce[jwt]'"
            ) from exc
        set_(self, "_signing_key", _signing_key(private_key, private_key_passphrase))

    @property
    def token_key(self) -> str:
        """``token_key`` if given, else a hash of the domain, client id and username."""
        return (
            self.key
            or f"jwt_bearer:{_short_hash(self.base_url + '|' + self.client_id + '|' + self.username)}"
        )

    @property
    def aud(self) -> str:
        """The ``aud`` claim. ``audience`` wins when set; otherwise it's inferred from ``domain``.

        - An Experience Cloud site (a ``domain`` with a path prefix, or a ``*.my.site.com``
          host): the site URL itself.
        - ``test``, ``https://test.salesforce.com`` or a ``*.sandbox.my.salesforce.com`` host:
          ``https://test.salesforce.com``.
        - Anything else: ``https://login.salesforce.com``.

        Salesforce's JWT bearer flow page names the Experience Cloud site URL (for example
        ``https://site.force.com/customers``) as the ``aud`` for site users:
        https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_jwt_flow.htm&type=5
        """
        if self.audience is None:
            base = resolve_domain(self.domain)
            parsed = urlsplit(base)
            # Decide on the parsed host, so "test" and "https://test.salesforce.com" agree.
            host = (parsed.hostname or "").lower()
            if parsed.path or host.endswith(".my.site.com"):
                return base
            sandbox = host == "test.salesforce.com" or host.endswith(".sandbox.my.salesforce.com")
            return _HOSTS["test" if sandbox else "login"]
        return _HOSTS.get(self.audience, self.audience)

    def assertion(self, now: float | None = None) -> str:
        """A freshly signed RS256 JWT assertion (``iss``, ``sub``, ``aud``, ``exp``)."""
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
        token: Any = jwt.encode(claims, self._signing_key, algorithm="RS256")  # pyright: ignore
        return token if isinstance(token, str) else token.decode()

    def token_form(self, current: Token | None) -> dict[str, str]:
        """JWT bearer grant form with a new assertion."""
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
        set_(self, "instance_url", check_instance_url(instance_url))
        set_(self, "domain", "login")
        set_(self, "client_id", "")
        set_(self, "client_secret", None)
        set_(self, "refresh_ahead", None)
        set_(self, "expiry", "unknown")

    @property
    def token_key(self) -> str:
        """A hash of the instance URL and the token."""
        return f"static:{_short_hash(self.instance_url + '|' + self.access_token)}"

    @property
    def client_id_hash(self) -> str:
        """Always empty: a static token has no OAuth client."""
        return ""

    def static_token(self) -> Token | None:
        """The token, built from the constructor arguments."""
        return Token(
            access_token=self.access_token,
            instance_url=self.instance_url,
            issued_at=datetime.now(timezone.utc),
        )

    def token_form(self, current: Token | None) -> dict[str, str]:  # pragma: no cover
        """Not supported: a static token can't be refreshed."""
        raise NotImplementedError("StaticToken can't be refreshed")
