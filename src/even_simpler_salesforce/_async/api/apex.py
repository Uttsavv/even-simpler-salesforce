"""Apex REST: ``sf.apex`` (§9.7)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..client import AsyncSalesforce

__all__ = ["AsyncApex"]

_PREFIX = "/services/apexrest"


def _apex_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/"):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"Apex REST path must start with '/', got {path!r}")
    if path.startswith(_PREFIX + "/") or path == _PREFIX:
        raise ValueError("pass the path after /services/apexrest, e.g. '/orders/submit'")
    if "?" in path:
        raise ValueError("pass query parameters with params=, not in the path")
    return _PREFIX + path


class AsyncApex:
    """Call your own Apex REST classes (``@RestResource``) under ``/services/apexrest``.

    Each method returns what the class returned: decoded JSON, ``str`` for a text body,
    ``bytes`` for a binary body, or ``None`` for an empty body. If the call fails, it raises
    ``ApexError`` with the decoded body in ``.body``. Auth, limit and gateway errors keep their
    own classes.

    Apex REST runs code, so the client never resends a call after it may have reached
    Salesforce, not even a ``GET``. Pass ``idempotent=True`` for a method you know is safe to
    run twice.
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        """``GET /services/apexrest{path}``.

        Args:
            path: Path after ``/services/apexrest``, starting with ``/`` (``"/orders/42"``).
            params: Query-string parameters.
            headers: Extra request headers.
            idempotent: ``True`` lets the client resend the call after a timeout or 5xx.
        """
        return await self._call("GET", path, params, None, headers, idempotent)

    async def post(
        self,
        path: str,
        json: Any = None,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        """``POST /services/apexrest{path}`` with a JSON body.

        Args:
            path: Path after ``/services/apexrest``, starting with ``/``.
            json: Request body, encoded as JSON.
            params: Query-string parameters.
            headers: Extra request headers.
            idempotent: ``True`` lets the client resend the call after a timeout or 5xx.
        """
        return await self._call("POST", path, params, json, headers, idempotent)

    async def put(
        self,
        path: str,
        json: Any = None,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        """``PUT /services/apexrest{path}`` with a JSON body. Arguments as in :meth:`post`."""
        return await self._call("PUT", path, params, json, headers, idempotent)

    async def patch(
        self,
        path: str,
        json: Any = None,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        """``PATCH /services/apexrest{path}`` with a JSON body. Arguments as in :meth:`post`."""
        return await self._call("PATCH", path, params, json, headers, idempotent)

    async def delete(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        """``DELETE /services/apexrest{path}``. Arguments as in :meth:`get`."""
        return await self._call("DELETE", path, params, None, headers, idempotent)

    async def _call(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        json: Any,
        headers: Mapping[str, str] | None,
        idempotent: bool | None,
    ) -> Any:
        return await self._sf.request(
            method,
            _apex_path(path),
            params=params,
            json=json,
            headers=headers,
            idempotent=idempotent,
            operation=f"apex.{method.lower()}",
        )
