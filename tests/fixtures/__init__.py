"""Fixture loader for ``tests/fixtures/recorded``.

A file there is one of two kinds:

- a **recording** made with ``esf.testing.record`` against a real org (``{"esf_fixture": 1,
  "interactions": [...]}``, redacted by the recorder). :func:`load_fixture` returns the response
  JSON of one ``/services/data`` interaction (``index``, default the first);
- a **synthetic** response (spec §18.4), hand-written from the Salesforce documentation because
  no recording exists yet: ``{"synthetic": true, "source": ..., "body": ...}``.
  :func:`load_fixture` returns ``body``.

Any other file is refused, so an unmarked hand-written response can't pass as a recording.
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
    data = json.loads((RECORDED / name).read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("esf_fixture") == 1 and isinstance(data.get("interactions"), list):
        return data
    if not (isinstance(data, dict) and data.get("synthetic") is True and "body" in data):
        raise ValueError(
            f"fixture {name} is neither a recording nor marked synthetic (see tests/fixtures/__init__.py)"
        )
    return data


def is_recorded(name: str) -> bool:
    """Whether the fixture is a real-org recording (not synthetic)."""
    return "interactions" in _read(name)


def load_fixture(name: str, index: int = 0) -> Any:
    """Return a deep copy of the fixture's response body.

    Args:
        name: Path below ``tests/fixtures/recorded``.
        index: For a recording, which ``/services/data`` interaction (token requests and
            other paths are skipped). Ignored for a synthetic fixture.
    """
    data = _read(name)
    if "interactions" not in data:
        return copy.deepcopy(data["body"])
    calls = [i for i in data["interactions"] if str(i["request"]["path"]).startswith("/services/data/")]
    return copy.deepcopy(calls[index]["response"].get("json"))


def load_request(name: str, index: int = 0) -> Any:
    """The JSON request body of a recorded ``/services/data`` interaction."""
    data = _read(name)
    calls = [i for i in data["interactions"] if str(i["request"]["path"]).startswith("/services/data/")]
    return copy.deepcopy(calls[index]["request"].get("json"))


def all_fixtures() -> list[str]:
    return sorted(p.relative_to(RECORDED).as_posix() for p in RECORDED.rglob("*.json"))
