"""Fixture loader for ``tests/fixtures/recorded``.

A file there is one of two kinds:

- **recorded**: a fixture written by ``esf.testing.record`` against a real org (``{"esf_fixture":
  1, "source": ..., "interactions": [...]}``), redacted and reviewed before it was committed.
  ``source`` says when and how it was recorded and what was trimmed.
- **synthetic** (spec §18.4): hand-written from the Salesforce documentation where no recording
  exists yet, ``{"synthetic": true, "source": ..., "body": ...}``.

:func:`load_fixture` refuses any other file and returns the response body: ``body`` of a
synthetic file, or the ``json`` (else ``text``) of one recorded interaction.
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
            f"fixture {name} is neither recorded nor marked synthetic (see tests/fixtures/__init__.py)"
        )
    return data


def is_recorded(name: str) -> bool:
    """Whether ``name`` is a real-org recording (not a synthetic fixture)."""
    return "esf_fixture" in _read(name)


def load_response(name: str, interaction: int = 0) -> dict[str, Any]:
    """A deep copy of the ``response`` of one interaction of a recorded fixture."""
    data = _read(name)
    if "esf_fixture" not in data:
        raise ValueError(f"fixture {name} is synthetic; it has no recorded response")
    return copy.deepcopy(data["interactions"][interaction]["response"])


def load_fixture(name: str, interaction: int = 0) -> Any:
    """Return a deep copy of the fixture's response body.

    Args:
        name: Path under ``tests/fixtures/recorded``.
        interaction: For a recorded fixture, which exchange (0-based) to take the body from.
    """
    data = _read(name)
    if "esf_fixture" not in data:
        return copy.deepcopy(data["body"])
    response = load_response(name, interaction)
    if "json" in response:
        return response["json"]
    return response.get("text", "")


def all_fixtures() -> list[str]:
    return sorted(p.relative_to(RECORDED).as_posix() for p in RECORDED.rglob("*.json"))
