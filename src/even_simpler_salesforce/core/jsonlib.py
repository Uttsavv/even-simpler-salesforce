"""JSON encode/decode. Uses orjson when installed (``[speed]`` extra), else the stdlib."""

from __future__ import annotations

import json as _json
from collections.abc import Iterable
from typing import Any, cast

__all__ = ["BACKEND", "dumps", "loads"]

try:  # pragma: no cover - depends on the environment
    import orjson as _orjson  # pyright: ignore[reportMissingImports]
except ImportError:  # pragma: no cover
    _orjson = None


def _std_dumps(obj: Any) -> bytes:
    return _json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=_default).encode()


def _default(obj: Any) -> Any:
    import datetime as _dt
    import decimal

    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(cast("Iterable[Any]", obj))
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


if _orjson is not None:  # pragma: no cover - depends on the environment
    _backend = "orjson"

    def dumps(obj: Any) -> bytes:
        return _orjson.dumps(obj, default=_default)  # pyright: ignore

    def loads(data: bytes | str) -> Any:
        return _orjson.loads(data)  # pyright: ignore

else:
    _backend = "json"

    def dumps(obj: Any) -> bytes:
        return _std_dumps(obj)

    def loads(data: bytes | str) -> Any:
        return _json.loads(data)


BACKEND: str = _backend
