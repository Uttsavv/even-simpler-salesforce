"""Fixture loader for ``tests/fixtures/recorded``.

A file there is one of two kinds:

- **recorded**: written by ``esf.testing.record`` against a real org (redacted), in the
  recorder's format ``{"esf_fixture": 1, "interactions": [...]}``. :func:`load_fixture`
  returns the response body of one interaction (parsed JSON, or text for XML and the like).
- **synthetic**: hand-written from the Salesforce documentation where no recording exists
  yet (spec §18.4), as ``{"synthetic": true, "source": ..., "body": ...}``.
  :func:`load_fixture` returns ``body``.

Any other file is refused, so an unmarked hand-written fixture can't pass as a recording.
"""

from __future__ import annotations

import base64
import copy
import functools
import json
from pathlib import Path
from typing import Any

RECORDED = Path(__file__).parent / "recorded"


@functools.cache
def _read(name: str) -> dict[str, Any]:
    data = json.loads((RECORDED / name).read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("esf_fixture") == 1 and isinstance(data.get("interactions"), list):
        return data
    if not (isinstance(data, dict) and data.get("synthetic") is True and "body" in data):
        raise ValueError(
            f"fixture {name} is neither recorded nor marked synthetic (see tests/fixtures/__init__.py)"
        )
    return data


def is_recorded(name: str) -> bool:
    """Whether ``name`` is a real recording (not a synthetic fixture)."""
    return "esf_fixture" in _read(name)


def load_interaction(name: str, index: int = 0) -> dict[str, Any]:
    """A deep copy of interaction ``index`` (``{"request": ..., "response": ...}``) of a recording."""
    data = _read(name)
    if "esf_fixture" not in data:
        raise ValueError(f"fixture {name} is synthetic, not a recording")
    return copy.deepcopy(data["interactions"][index])


def load_fixture(name: str, index: int = 0) -> Any:
    """The response body of a fixture: ``body`` (synthetic) or interaction ``index`` (recorded).

    A recorded body is the parsed JSON, else the text (``None`` for an empty body).
    """
    data = _read(name)
    if "esf_fixture" not in data:
        return copy.deepcopy(data["body"])
    response = load_interaction(name, index)["response"]
    if "json" in response:
        return response["json"]
    return response.get("text")


def recorded_response(name: str, index: int = 0) -> tuple[int, dict[str, str], bytes]:
    """``(status, headers, body bytes)`` of interaction ``index`` of a recording, for a mock."""
    response = load_interaction(name, index)["response"]
    if "json" in response:
        body = json.dumps(response["json"]).encode()
    elif "base64" in response:
        body = base64.b64decode(response["base64"])
    else:
        body = str(response.get("text", "")).encode()
    return int(response["status"]), dict(response.get("headers") or {}), body


def all_fixtures() -> list[str]:
    return sorted(p.relative_to(RECORDED).as_posix() for p in RECORDED.rglob("*.json"))
