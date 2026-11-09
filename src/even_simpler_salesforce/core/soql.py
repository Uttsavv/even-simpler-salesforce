"""Safe SOQL building (§9.2) and SOQL normalisation for logs (§11.3)."""

from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import math
import re
import string
from typing import Any

__all__ = [
    "SoqlIdentifier",
    "SoqlLiteral",
    "quote",
    "redact_soql",
    "soql",
    "soql_hash",
    "soql_identifier",
    "soql_like",
]

_IDENT_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(__[a-z]+)?$")
_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}
_ESCAPE_RE = re.compile("|".join(re.escape(k) for k in _ESCAPES))


class SoqlLiteral(str):
    """A string that is already a complete, safely quoted SOQL literal.

    Only produced by :func:`soql_like` and :func:`quote`; inserted into :func:`soql` unchanged.
    """

    __slots__ = ()


class SoqlIdentifier(str):
    """A validated field or object name; inserted into :func:`soql` unquoted."""

    __slots__ = ()


def _escape(value: str) -> str:
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group(0)], value)


def _format_datetime(value: _dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime is ambiguous in SOQL; attach a timezone (e.g. UTC)")
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quote(value: Any) -> str:
    """Render one Python value as a SOQL literal.

    Raises:
        TypeError: unsupported type.
        ValueError: values SOQL can't express (NaN, naive datetimes, empty sequences).
    """
    if isinstance(value, (SoqlLiteral, SoqlIdentifier)):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f"'{_escape(value)}'"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN/Infinity can't be used in SOQL")
        text = repr(value)
        # SOQL numbers have no exponent form: 1e16 -> 10000000000000000, 1e-07 -> 0.0000001
        return format(decimal.Decimal(text), "f") if ("e" in text or "E" in text) else text
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise ValueError("NaN/Infinity can't be used in SOQL")
        return format(value, "f")
    if isinstance(value, _dt.datetime):
        return _format_datetime(value)
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        items: list[Any] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        if not items:
            raise ValueError("empty sequence can't be used in SOQL (IN () is invalid)")
        rendered: list[str] = []
        for item in items:
            if isinstance(item, (list, tuple, set, frozenset)):
                raise TypeError("nested sequences can't be used in SOQL")
            rendered.append(quote(item))
        return "(" + ",".join(rendered) + ")"
    raise TypeError(f"can't use a value of type {type(value).__name__} in SOQL")


class _Formatter(string.Formatter):
    def format_field(self, value: Any, format_spec: str) -> str:
        if format_spec == "like":
            if not isinstance(value, str):
                raise TypeError("{:like} needs a string")
            return soql_like(value)
        if format_spec:
            raise ValueError(f"unsupported SOQL placeholder format {format_spec!r} (only 'like')")
        return quote(value)

    def convert_field(self, value: Any, conversion: str | None) -> Any:
        if conversion is not None:
            raise ValueError("conversions (!r, !s, !a) are not allowed in SOQL placeholders")
        return value


_FORMATTER = _Formatter()


def soql(template: str, /, *args: Any, **kwargs: Any) -> str:
    """Build SOQL with safely quoted values.

    ``{}`` / ``{name}`` placeholders are replaced by quoted literals. Use ``{{``/``}}`` for
    literal braces. ``{:like}`` escapes ``%`` and ``_`` for exact matching inside ``LIKE``.

    Example:
        >>> soql("SELECT Id FROM Lead WHERE Status IN {} AND IsConverted = {}", ["Open"], False)
        "SELECT Id FROM Lead WHERE Status IN ('Open') AND IsConverted = false"

    Field and object names are not placeholders; pass them through :func:`soql_identifier`.
    """
    return _FORMATTER.vformat(template, args, kwargs)


def soql_like(value: str, pattern: str = "{}") -> SoqlLiteral:
    """Quote ``value`` for use after ``LIKE``, escaping the ``%`` and ``_`` wildcards.

    ``pattern`` adds your own wildcards around the escaped value: ``soql_like(v, "%{}%")``
    is a "contains" match.
    """
    if pattern.count("{}") != 1:
        raise ValueError("pattern must contain exactly one '{}'")
    escaped = _escape(value).replace("%", "\\%").replace("_", "\\_")
    before, after = pattern.split("{}")
    return SoqlLiteral(f"'{_escape(before)}{escaped}{_escape(after)}'")


def soql_identifier(name: str) -> SoqlIdentifier:
    """Validate a field/object name (dotted relationship paths allowed) for use in SOQL."""
    if not isinstance(name, str) or not name:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("identifier must be a non-empty string")
    for seg in name.split("."):
        if not _IDENT_SEGMENT.fullmatch(seg):
            raise ValueError(f"invalid SOQL identifier: {name!r}")
    return SoqlIdentifier(name)


# --- normalisation for logs ------------------------------------------------------------

_STRING_LIT = re.compile(r"'(?:\\.|[^'\\])*'")
_SOSL_TERM = re.compile(r"\{(?:\\.|[^}\\])*\}")  # FIND {search terms}
_NUM_LIT = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?(?![A-Za-z0-9_])")
_DATE_LIT = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?")
_WS = re.compile(r"\s+")


def redact_soql(query: str) -> str:
    """Replace string, date and number literals (and SOSL ``FIND {...}`` terms) with ``?``
    and collapse whitespace."""
    out = _SOSL_TERM.sub("{?}", query)
    out = _STRING_LIT.sub("?", out)
    out = _DATE_LIT.sub("?", out)
    out = _NUM_LIT.sub("?", out)
    return _WS.sub(" ", out).strip()


def soql_hash(query: str) -> str:
    """SHA-256 of the redacted, normalised SOQL; first 12 hex characters."""
    return hashlib.sha256(redact_soql(query).encode()).hexdigest()[:12]
