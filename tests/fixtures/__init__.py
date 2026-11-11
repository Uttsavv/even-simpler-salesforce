"""Fixture loader for ``tests/fixtures/recorded``.

A file there is one of two kinds:

- **recorded**: written by ``esf.testing.record`` against a real org (``{"esf_fixture": 1,
  "interactions": [...]}``, redacted). :func:`load_fixture` returns the response body of one
  interaction, :func:`load_response` its status and body.
- **synthetic** (spec §18.4): hand-written from the Salesforce documentation where no recording
  exists yet, ``{"synthetic": true, "source": ..., "body": ...}``. :func:`load_fixture` returns
  ``body``.

Any other file is refused. Replace synthetic files with recordings when a sandbox is available.
"""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path
from typing import Any

RECORDED = Path(__file__).parent / "recorded"


@functools.cache
def _read(name: str) -> dict[str, Any]:
    data = json.loads((RECORDED / name).read_text())
    if isinstance(data, dict) and data.get("esf_fixture") == 1 and data.get("interactions"):
        return data
    if not (isinstance(data, dict) and data.get("synthetic") is True and "body" in data):
        raise ValueError(f"fixture {name} is neither a recording nor marked synthetic (see tests/fixtures)")
    return data


def is_recorded(name: str) -> bool:
    """``True`` for a recording written by ``esf.testing.record``."""
    return "esf_fixture" in _read(name)


def load_response(name: str, index: int = 0) -> tuple[int, Any]:
    """``(status, body)`` of interaction ``index`` of a recorded fixture (a deep copy)."""
    data = _read(name)
    if "esf_fixture" not in data:
        raise ValueError(f"fixture {name} is synthetic: it has no recorded status")
    response = data["interactions"][index]["response"]
    return int(response["status"]), copy.deepcopy(response.get("json"))


def load_fixture(name: str, index: int = 0) -> Any:
    """Return a deep copy of the fixture's ``body`` (of interaction ``index`` for a recording)."""
    data = _read(name)
    if "esf_fixture" in data:
        return load_response(name, index)[1]
    return copy.deepcopy(data["body"])


def all_fixtures() -> list[str]:
    return sorted(p.relative_to(RECORDED).as_posix() for p in RECORDED.rglob("*.json"))
