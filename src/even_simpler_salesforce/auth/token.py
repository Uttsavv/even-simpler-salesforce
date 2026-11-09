"""The :class:`Token` value object and OAuth token-response parsing. No I/O."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..errors import UnexpectedResponse

__all__ = ["Token", "merge_token", "parse_token_response"]


def _redact(value: str | None) -> str | None:
    if value is None:
        return None
    return "…" + value[-4:] if len(value) > 8 else "…"


@dataclass(frozen=True, slots=True)
class Token:
    """An OAuth token as issued by Salesforce.

    ``repr()`` redacts ``access_token`` and ``refresh_token``.
    """

    access_token: str
    instance_url: str
    issued_at: datetime
    refresh_token: str | None = None
    id_url: str | None = None
    org_id: str | None = None
    user_id: str | None = None
    scope: frozenset[str] = frozenset()
    signature: str | None = None
    expires_at: datetime | None = None
    version: int = 0

    def __repr__(self) -> str:
        return (
            f"Token(access_token={_redact(self.access_token)!r}, instance_url={self.instance_url!r}, "
            f"issued_at={self.issued_at.isoformat()!r}, refresh_token={_redact(self.refresh_token)!r}, "
            f"org_id={self.org_id!r}, user_id={self.user_id!r}, expires_at="
            f"{self.expires_at.isoformat() if self.expires_at else None!r}, version={self.version})"
        )

    __str__ = __repr__

    def replace(self, **changes: Any) -> Token:
        return dataclasses.replace(self, **changes)


def _parse_id_url(id_url: str | None) -> tuple[str | None, str | None]:
    if not id_url:
        return None, None
    parts = [p for p in urlparse(id_url).path.split("/") if p]
    if len(parts) >= 3 and parts[-3] == "id":
        return parts[-2], parts[-1]
    return None, None


def _parse_issued_at(value: Any, now: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return now


def parse_token_response(data: Mapping[str, Any], *, now: datetime | None = None) -> Token:
    """Build a :class:`Token` from a ``/services/oauth2/token`` JSON response."""
    now = now or datetime.now(timezone.utc)
    access = data.get("access_token")
    instance = data.get("instance_url")
    if not isinstance(access, str) or not isinstance(instance, str):
        raise UnexpectedResponse("token response lacks access_token or instance_url")
    id_url = data.get("id")
    org_id, user_id = _parse_id_url(id_url if isinstance(id_url, str) else None)
    scope_raw = data.get("scope")
    scope = frozenset(scope_raw.split()) if isinstance(scope_raw, str) else frozenset[str]()
    refresh = data.get("refresh_token")
    sig = data.get("signature")
    return Token(
        access_token=access,
        instance_url=instance.rstrip("/"),
        issued_at=_parse_issued_at(data.get("issued_at"), now),
        refresh_token=refresh if isinstance(refresh, str) else None,
        id_url=id_url if isinstance(id_url, str) else None,
        org_id=org_id,
        user_id=user_id,
        scope=scope,
        signature=sig if isinstance(sig, str) else None,
    )


def merge_token(new: Token, previous: Token | None) -> Token:
    """Keep the previous refresh token (and identity) when the response carries none."""
    if previous is None:
        return new
    return new.replace(
        refresh_token=new.refresh_token or previous.refresh_token,
        id_url=new.id_url or previous.id_url,
        org_id=new.org_id or previous.org_id,
        user_id=new.user_id or previous.user_id,
        scope=new.scope or previous.scope,
        version=previous.version,
    )
