"""Fixture loader for ``tests/fixtures/recorded``.

Two kinds of file (spec §18.4):

- **recorded**: written by ``esf.testing.record`` against a real org (``{"esf_fixture": 1,
  "interactions": [...]}``), plus a ``source`` note saying which sandbox test recorded it.
  :func:`load_fixture` returns the JSON body of the **last** interaction's response (a recording
  may start with the request that set the record up); :func:`load_interactions` returns them all.
- **synthetic**: hand-written from the Salesforce documentation, ``{"synthetic": true,
  "source": ..., "body": ...}``. :func:`load_fixture` returns ``body``.

Anything else is refused. Replace synthetic files with recordings when a sandbox can produce them.
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
        return {
            "body": data["interactions"][-1]["response"].get("json"),
            "interactions": data["interactions"],
        }
    if not (isinstance(data, dict) and data.get("synthetic") is True and "body" in data):
        raise ValueError(
            f"fixture {name} is neither recorded nor marked synthetic (see tests/fixtures/__init__.py)"
        )
    return data


def load_fixture(name: str) -> Any:
    """Return a deep copy of the fixture's response body."""
    return copy.deepcopy(_read(name)["body"])


def load_interactions(name: str) -> list[dict[str, Any]]:
    """Return a deep copy of a recorded fixture's interactions (``request`` / ``response``)."""
    data = _read(name)
    if "interactions" not in data:
        raise ValueError(f"fixture {name} is synthetic, not recorded")
    return copy.deepcopy(data["interactions"])


def all_fixtures() -> list[str]:
    return sorted(p.relative_to(RECORDED).as_posix() for p in RECORDED.rglob("*.json"))
