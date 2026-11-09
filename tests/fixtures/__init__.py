"""Fixture loader for ``tests/fixtures/recorded``.

Every file there is **synthetic** (spec §18.4): hand-written from the Salesforce documentation
because no real-org recordings exist yet. Each file is ``{"synthetic": true, "source": ...,
"body": ...}``; :func:`load_fixture` refuses files that are not marked and returns ``body``.
Replace them with redacted recordings (``esf.testing.record``) once a sandbox is available.
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
