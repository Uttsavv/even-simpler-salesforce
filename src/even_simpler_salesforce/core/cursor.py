"""Opaque query cursors that are safe to hand to a frontend (§9.2, O9).

A cursor is the base64url-encoded ``nextRecordsUrl`` path, optionally followed by ``.`` and an
HMAC-SHA256 tag. Decoding validates the path against the shape of a real query locator so a
client-supplied cursor can't make the server call arbitrary URLs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re

from ..errors import MalformedQuery

__all__ = ["Cursor", "decode_cursor", "encode_cursor"]

_LOCATOR_PATH = re.compile(r"^/services/data/v\d+\.\d/query(All)?/[0-9A-Za-z]{15,18}-\d+$")


class Cursor(str):
    """Opaque pagination cursor. Treat it as a string; don't parse it."""

    __slots__ = ()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _tag(key: bytes, payload: str) -> str:
    return _b64e(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def encode_cursor(next_records_url: str, key: bytes | None = None) -> Cursor:
    """Encode a ``nextRecordsUrl`` path as a :class:`Cursor`."""
    if not _LOCATOR_PATH.match(next_records_url):
        raise MalformedQuery(f"unexpected nextRecordsUrl shape: {next_records_url!r}")
    payload = _b64e(next_records_url.encode("ascii"))
    if key is not None:
        return Cursor(f"{payload}.{_tag(key, payload)}")
    return Cursor(payload)


def decode_cursor(cursor: str, key: bytes | None = None) -> str:
    """Validate and decode a cursor back into a query-locator path.

    Raises:
        MalformedQuery: the cursor is malformed, tampered with, or signed with another key.
    """
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise MalformedQuery("invalid cursor", error_code="INVALID_CURSOR")
    payload, _, tag = cursor.partition(".")
    if key is not None:
        if not tag or not hmac.compare_digest(tag, _tag(key, payload)):
            raise MalformedQuery("cursor signature mismatch", error_code="INVALID_CURSOR")
    elif tag:
        raise MalformedQuery("signed cursor given but no cursor_key configured", error_code="INVALID_CURSOR")
    try:
        path = _b64d(payload).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise MalformedQuery("invalid cursor", error_code="INVALID_CURSOR") from None
    if not _LOCATOR_PATH.match(path):
        raise MalformedQuery("invalid cursor", error_code="INVALID_CURSOR")
    return path
