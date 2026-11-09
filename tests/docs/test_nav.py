"""Every page in the ``zensical.toml`` nav exists, and every docs page is in the nav."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    tomllib = pytest.importorskip("tomli")


def _nav_pages(items: list[Any]) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for value in item.values():
                out.extend(_nav_pages(value) if isinstance(value, list) else [value])
    return out


def _nav() -> list[str]:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))
    return _nav_pages(config["project"]["nav"])


def test_nav_pages_exist() -> None:
    missing = [p for p in _nav() if not (DOCS / p).is_file()]
    assert not missing, missing


def test_every_page_is_in_the_nav() -> None:
    nav = set(_nav())
    pages = {str(p.relative_to(DOCS)) for p in DOCS.rglob("*.md")}
    orphans = sorted(pages - nav - {"adr/" + p.name for p in (DOCS / "adr").glob("0*.md")})
    assert not orphans, orphans
