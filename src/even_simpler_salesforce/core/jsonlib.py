"""JSON encode/decode. Uses orjson when installed (``[speed]`` extra), else the stdlib.

Both backends behave identically:

- NaN and ±Infinity (``float`` or ``Decimal``, at any depth) raise ``ValueError`` before
  anything is sent. orjson would otherwise turn them into ``null`` and silently blank the field
  in Salesforce; the stdlib would emit ``NaN``, which is not JSON.
- ``Decimal`` is encoded as a JSON *number* with its exact digits (never via ``float``).
- ``datetime``/``date`` become ISO 8601 strings; ``set``/``frozenset``/``tuple`` become arrays.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import json as _json
import math
import re
import secrets
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

__all__ = ["BACKEND", "NonFiniteNumber", "dumps", "loads", "orjson_dumps", "std_dumps", "std_loads"]

try:
    import orjson as _orjson  # pyright: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - depends on the environment
    _orjson = None

_orjson_mod: Any = _orjson


class NonFiniteNumber(ValueError):
    """A NaN/Infinity value can't be sent to Salesforce."""


def _check(obj: Any, path: str = "$") -> None:
    """Raise :class:`NonFiniteNumber` for NaN/±Inf anywhere in ``obj``."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise NonFiniteNumber(f"{obj!r} at {path} can't be sent to Salesforce (not valid JSON)")
    elif isinstance(obj, decimal.Decimal):
        if not obj.is_finite():
            raise NonFiniteNumber(f"Decimal({str(obj)!r}) at {path} can't be sent to Salesforce")
    elif isinstance(obj, Mapping):
        for k, v in cast("Mapping[Any, Any]", obj).items():
            _check(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for i, v in enumerate(cast("Iterable[Any]", obj)):
            _check(v, f"{path}[{i}]")


def _common_default(obj: Any) -> Any:
    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(cast("Iterable[Any]", obj))
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# --- stdlib backend ----------------------------------------------------------------------

_NONCE = secrets.token_hex(8)
_DEC_PREFIX = f"\x00esf-dec-{_NONCE}:"
_DEC_RE = re.compile(r'"\\u0000esf-dec-' + _NONCE + r':([^"\\]+)"')


def _std_default(obj: Any) -> Any:
    if isinstance(obj, decimal.Decimal):
        return _DEC_PREFIX + str(obj)  # placeholder, swapped for a bare number below
    return _common_default(obj)


def std_dumps(obj: Any) -> bytes:
    """Encode with the standard library."""
    _check(obj)
    text = _json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=_std_default)
    if _DEC_PREFIX[1:] in text:
        text = _DEC_RE.sub(lambda m: m.group(1), text)
    return text.encode()


def std_loads(data: bytes | str) -> Any:
    """Decode with the standard library."""
    return _json.loads(data)


# --- orjson backend ----------------------------------------------------------------------


def _orjson_default(obj: Any) -> Any:
    if isinstance(obj, decimal.Decimal):
        return _orjson_mod.Fragment(str(obj).encode())
    return _common_default(obj)


def orjson_dumps(obj: Any) -> bytes:
    """Encode with orjson (needs the ``[speed]`` extra)."""
    if _orjson_mod is None:  # pragma: no cover - depends on the environment
        raise ImportError("orjson is not installed; install the [speed] extra")
    _check(obj)
    return cast(bytes, _orjson_mod.dumps(obj, default=_orjson_default))


def orjson_loads(data: bytes | str) -> Any:
    """Decode with orjson (needs the ``[speed]`` extra)."""
    if _orjson_mod is None:  # pragma: no cover - depends on the environment
        raise ImportError("orjson is not installed; install the [speed] extra")
    return _orjson_mod.loads(data)


# --- selection ---------------------------------------------------------------------------

dumps: Callable[[Any], bytes]
loads: Callable[[bytes | str], Any]
BACKEND: str = "orjson" if _orjson_mod is not None else "json"
if _orjson_mod is not None:
    dumps, loads = orjson_dumps, orjson_loads
else:  # pragma: no cover - depends on the environment
    dumps, loads = std_dumps, std_loads
