"""Fixture loader for ``tests/fixtures/recorded``.

Two kinds of files live there:

- **recorded**: written by ``esf.testing.record`` against a real org (recorder format
  ``{"esf_fixture": 1, "interactions": [...]}``, optionally with a ``note``), redacted and
  reviewed before commit. :func:`load_fixture` returns the JSON body of one response.
- **synthetic** (spec §18.4): hand-written from the Salesforce documentation where no
  recording exists yet, ``{"synthetic": true, "source": ..., "body": ...}``.

Any other file is refused, so an unmarked hand-written response can't slip in.
"""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path
from typing import Any

RECORDED = Path(__file__).parent / "recorded"
_FIXTURE_VERSION = 1


@functools.cache
def _read(name: str) -> dict[str, Any]:
    data = json.loads((RECORDED / name).read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("esf_fixture") == _FIXTURE_VERSION:
        if not isinstance(data.get("interactions"), list) or not data["interactions"]:
            raise ValueError(f"fixture {name} has no interactions")
        return data
    if not (isinstance(data, dict) and data.get("synthetic") is True and "body" in data):
        raise ValueError(
            f"fixture {name} is neither recorded nor marked synthetic (see tests/fixtures/__init__.py)"
        )
    return data


def is_synthetic(name: str) -> bool:
    """Whether the fixture is hand-written rather than recorded."""
    return _read(name).get("synthetic") is True


def load_fixture(name: str, index: int = 0) -> Any:
    """Return a deep copy of the fixture's body (of response ``index`` for a recorded file)."""
    data = _read(name)
    if is_synthetic(name):
        return copy.deepcopy(data["body"])
    return copy.deepcopy(data["interactions"][index]["response"].get("json"))


def fixture_status(name: str, index: int = 0) -> int:
    """The HTTP status of recorded response ``index`` (200 for a synthetic fixture)."""
    data = _read(name)
    if is_synthetic(name):
        return 200
    return int(data["interactions"][index]["response"]["status"])


def all_fixtures() -> list[str]:
    return sorted(p.relative_to(RECORDED).as_posix() for p in RECORDED.rglob("*.json"))
