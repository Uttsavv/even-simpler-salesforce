"""Sandbox configuration from ``ESF_SANDBOX_*`` environment variables (no pytest needed).

Shared by ``tests/sandbox`` and ``scripts/verify_sandbox.py`` / ``scripts/sandbox_login.py``.
See ``tests/sandbox/README.md`` for the full list of variables.

Two ways to log in:

- **web-server mode** (needed for the rotation test): ``ESF_SANDBOX_DOMAIN`` +
  ``ESF_SANDBOX_CLIENT_ID`` (+ optional ``ESF_SANDBOX_CLIENT_SECRET``) and either
  ``ESF_SANDBOX_REFRESH_TOKEN`` or a token already saved in the token database by
  ``scripts/sandbox_login.py``. The refresh token lives in a SQLite token store
  (``ESF_SANDBOX_TOKEN_DB``) so that a rotated refresh token survives between runs.
- **env mode**: anything the package's own ``esf_sandbox`` marker understands
  (``ACCESS_TOKEN`` + ``INSTANCE_URL``, client credentials, or JWT bearer).

Web-server mode wins when both are configured.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import even_simpler_salesforce as esf
from even_simpler_salesforce.cli._env import EnvConfigError, auth_from_env
from even_simpler_salesforce.testing.plugin import SANDBOX_PREFIX

from .sqlite_store import AsyncSqliteTokenStore, SqliteTokenStore

__all__ = ["PLACEHOLDER_ACCESS_TOKEN", "Settings", "load_settings"]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOKEN_DB = REPO_ROOT / ".esf-sandbox" / "tokens.sqlite3"
DEFAULT_TOKEN_KEY = "esf-sandbox:web_server"
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
PLACEHOLDER_ACCESS_TOKEN = "esf-sandbox-unprimed"  # noqa: S105 - not a secret
SETUP_HINT = "see tests/sandbox/README.md"


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(SANDBOX_PREFIX + name, "").strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(SANDBOX_PREFIX + name, "").strip()
    return int(raw) if raw else default


def _get(env: Mapping[str, str], name: str) -> str | None:
    return env.get(SANDBOX_PREFIX + name, "").strip() or None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    """Parsed sandbox settings. ``mode is None`` means "not configured" (see ``reason``)."""

    mode: str | None  # "web_server" | "env" | None
    reason: str
    domain: str | None = None
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    redirect_uri: str = DEFAULT_REDIRECT_URI
    refresh_token: str | None = field(default=None, repr=False)
    instance_url: str | None = None
    token_db: Path = DEFAULT_TOKEN_DB
    token_key: str = DEFAULT_TOKEN_KEY
    api_version: str | None = None
    allow_writes: bool = False  # heavy writes (>200 rows)
    read_only: bool = False  # no writes at all
    external_id: tuple[str, str] | None = None  # (sobject, field)
    rotation_cycles: int = 1000
    rotation_verify_every: int = 50
    rotation_pause: float = 0.0
    sweep: bool = False
    env: Mapping[str, str] = field(default_factory=lambda: {}, repr=False)

    @property
    def configured(self) -> bool:
        return self.mode is not None

    @property
    def web_server(self) -> bool:
        return self.mode == "web_server"

    @property
    def skip_reason(self) -> str:
        return f"sandbox: {self.reason}; {SETUP_HINT}"

    # --- auth & clients ---------------------------------------------------------------

    def flow(self) -> esf.WebServerFlow:
        if not (self.domain and self.client_id):
            raise RuntimeError("web-server mode needs ESF_SANDBOX_DOMAIN and ESF_SANDBOX_CLIENT_ID")
        return esf.WebServerFlow(
            domain=self.domain,
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            token_key=self.token_key,
        )

    def auth(self) -> esf.Auth:
        if self.web_server:
            return self.flow()
        return auth_from_env(self.env, SANDBOX_PREFIX)

    def sync_store(self) -> SqliteTokenStore:
        return SqliteTokenStore(self.token_db)

    def client_kwargs(self, **kw: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"client_name": "esf-sandbox"}
        if self.api_version:
            out["api_version"] = self.api_version
        out.update(kw)
        return out

    def store_for(self, kind: str, memory: dict[str, Any]) -> Any:
        """The token store for a client flavour. ``memory`` caches in-memory stores per kind."""
        if self.web_server:
            return (
                AsyncSqliteTokenStore(self.token_db) if kind == "async" else SqliteTokenStore(self.token_db)
            )
        if kind not in memory:
            memory[kind] = esf.AsyncInMemoryTokenStore() if kind == "async" else esf.InMemoryTokenStore()
        return memory[kind]

    def sync_client(self, **kw: Any) -> esf.Salesforce:
        store = self.sync_store() if self.web_server else kw.pop("store", None)
        return esf.Salesforce(self.auth(), store=store, **self.client_kwargs(**kw))

    # --- web-server priming -----------------------------------------------------------

    def prime(self) -> None:
        """Make sure the token DB holds a usable token (web-server mode only).

        If ``ESF_SANDBOX_REFRESH_TOKEN`` changed since the last run, it is (re)seeded into the
        store; a seeded token is exchanged for a real one with a single refresh. Afterwards the
        store, not the env var, is the source of truth: with rotation on, the env value is
        already rotated out after the first refresh.
        """
        if not self.web_server:
            return
        store = self.sync_store()
        meta = f"seeded:{self.token_key}"
        if self.refresh_token and store.get_meta(meta) != _hash(self.refresh_token):
            base = self.instance_url or self.flow().base_url
            store.save(
                self.token_key,
                esf.Token(
                    access_token=PLACEHOLDER_ACCESS_TOKEN,
                    instance_url=base,
                    issued_at=datetime.now(timezone.utc),
                    refresh_token=self.refresh_token,
                ),
                expected_version=None,
            )
            store.set_meta(meta, _hash(self.refresh_token))
        current = store.load(self.token_key)
        if current is not None and current.access_token == PLACEHOLDER_ACCESS_TOKEN:
            with esf.Salesforce(self.flow(), store=store, **self.client_kwargs()) as sf:
                sf._runtime.refresh(current)  # pyright: ignore[reportPrivateUsage]


def _stored_token_exists(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    try:
        return SqliteTokenStore(path).load(key) is not None
    except Exception:
        return False


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read ``ESF_SANDBOX_*`` from ``env`` (default: ``os.environ``)."""
    env = dict(os.environ if env is None else env)
    common: dict[str, Any] = {
        "domain": _get(env, "DOMAIN"),
        "client_id": _get(env, "CLIENT_ID"),
        "client_secret": _get(env, "CLIENT_SECRET"),
        "redirect_uri": _get(env, "REDIRECT_URI") or DEFAULT_REDIRECT_URI,
        "refresh_token": _get(env, "REFRESH_TOKEN"),
        "instance_url": _get(env, "INSTANCE_URL"),
        "token_db": Path(_get(env, "TOKEN_DB") or DEFAULT_TOKEN_DB).expanduser(),
        "token_key": _get(env, "TOKEN_KEY") or DEFAULT_TOKEN_KEY,
        "api_version": _get(env, "API_VERSION"),
        "allow_writes": _flag(env, "ALLOW_WRITES"),
        "read_only": _flag(env, "READ_ONLY"),
        "rotation_cycles": _int(env, "ROTATION_CYCLES", 1000),
        "rotation_verify_every": _int(env, "ROTATION_VERIFY_EVERY", 50),
        "rotation_pause": _int(env, "ROTATION_PAUSE_MS", 0) / 1000,
        "sweep": _flag(env, "SWEEP"),
        "env": env,
    }
    ext = _get(env, "EXTERNAL_ID_FIELD")
    if ext:
        obj, _, fld = ext.partition(".")
        if not (obj and fld):
            return Settings(None, "ESF_SANDBOX_EXTERNAL_ID_FIELD must look like Lead.Ext__c", **common)
        common["external_id"] = (obj, fld)

    has_web = bool(common["domain"] and common["client_id"]) and bool(
        common["refresh_token"] or _stored_token_exists(common["token_db"], common["token_key"])
    )
    if has_web:
        return Settings("web_server", "web-server flow with a stored refresh token", **common)
    try:
        auth_from_env(env, SANDBOX_PREFIX, read_key=False)
    except EnvConfigError as exc:
        if not any(k.startswith(SANDBOX_PREFIX) and v.strip() for k, v in env.items()):
            return Settings(None, "no org configured (no ESF_SANDBOX_* variables set)", **common)
        return Settings(None, f"incomplete ESF_SANDBOX_* login: {exc}", **common)
    return Settings("env", "login from ESF_SANDBOX_* (esf_sandbox plugin rules)", **common)
