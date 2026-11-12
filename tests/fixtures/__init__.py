"""Fixture loader for ``tests/fixtures/recorded``.

Two kinds of file live there:

- **synthetic** (spec §18.4): hand-written from the Salesforce documentation,
  ``{"synthetic": true, "source": ..., "body": ...}``. :func:`load_fixture` refuses files that
  are not marked and returns ``body``.
- **recorded**: redacted real-org exchanges written by ``esf.testing.record`` (``{"esf_fixture":
  1, "interactions": [...]}``). Replay them with ``esf.testing.replay(recording(name))``, or
  pick one response body with :func:`recorded_body`.
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
    if not (isinstance(data, dict) and data.get("synthetic") is True and "body" in data):
        raise ValueError(f"fixture {name} is not marked synthetic (see tests/fixtures/__init__.py)")
    return data


def load_fixture(name: str) -> Any:
    """Return a deep copy of the fixture's ``body``."""
    return copy.deepcopy(_read(name)["body"])


def all_fixtures() -> list[str]:
    return sorted(p.relative_to(RECORDED).as_posix() for p in RECORDED.rglob("*.json"))


def recording(name: str) -> Path:
    """The path of a recorded fixture (``esf.testing.record`` format), checked."""
    path = RECORDED / name
    data = json.loads(path.read_text(encoding="utf-8"))
    if not (
        isinstance(data, dict) and data.get("esf_fixture") == 1 and isinstance(data.get("interactions"), list)
    ):
        raise ValueError(f"fixture {name} is not an esf.testing.record recording")
    return path


def recorded_body(name: str, method: str, path_suffix: str, index: int = 0) -> Any:
    """The response body of the ``index``-th recorded ``method`` request whose path ends with
    ``path_suffix`` (JSON parsed, otherwise the text)."""
    data = json.loads(recording(name).read_text(encoding="utf-8"))
    found = [
        i["response"]
        for i in data["interactions"]
        if i["request"]["method"] == method and i["request"]["path"].endswith(path_suffix)
    ]
    response = found[index]
    return copy.deepcopy(response["json"]) if "json" in response else response.get("text", "")
