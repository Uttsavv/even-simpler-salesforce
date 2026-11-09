"""Every page in the ``zensical.toml`` nav exists, and every docs page is in the nav.

The changelog and the ADRs are the exception: they are linked from the header
(``overrides/partials/header.html``), not the sidebar, and each ADR is linked from
``adr/index.md``.
"""

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


HEADER = ROOT / "overrides" / "partials" / "header.html"


def _outside_nav(page: str) -> bool:
    return page == "changelog.md" or page.startswith("adr/")


def test_every_page_is_in_the_nav() -> None:
    nav = set(_nav())
    pages = {p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*.md")}  # nav uses "/" on every OS
    orphans = sorted(p for p in pages - nav if not _outside_nav(p))
    assert not orphans, orphans


def test_pages_outside_the_nav_are_linked() -> None:
    header = HEADER.read_text(encoding="utf-8")
    assert "'changelog/' | url" in header
    assert "'adr/' | url" not in header  # ADRs are linked from the API reference, not the header
    reference = (DOCS / "reference" / "index.md").read_text(encoding="utf-8")
    assert "../adr/index.md" in reference
    index = (DOCS / "adr" / "index.md").read_text(encoding="utf-8")
    adrs = sorted(p.name for p in (DOCS / "adr").glob("*.md") if p.name != "index.md")
    unlinked = [name for name in adrs if f"({name})" not in index]
    assert not unlinked, unlinked


def test_changelog_include_starts_at_the_towncrier_marker() -> None:
    page = (DOCS / "changelog.md").read_text(encoding="utf-8")
    line = int(page.split('--8<-- "CHANGELOG.md:', 1)[1].split(":", 1)[0])
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    assert changelog[line - 1] == "<!-- towncrier release notes start -->"
