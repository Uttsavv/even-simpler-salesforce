"""``docs/errors.md`` shows every public exception class with its real parent."""

from __future__ import annotations

import re
from pathlib import Path

import even_simpler_salesforce.errors as E

PAGE = Path(__file__).resolve().parents[2] / "docs" / "errors.md"


def _tree() -> dict[str, str | None]:
    text = PAGE.read_text(encoding="utf-8")
    block = text.split("```text", 1)[1].split("```", 1)[0]
    parents: dict[int, str] = {}
    out: dict[str, str | None] = {}
    for line in block.strip().splitlines():
        m = re.match(r"^(?P<prefix>[│ ├└─]*)(?P<name>[A-Za-z]+)", line)
        assert m, line
        depth = len(m.group("prefix")) // 4
        name = m.group("name")
        out[name] = parents.get(depth - 1)
        parents[depth] = name
    return out


def test_hierarchy_matches_errors_module() -> None:
    tree = _tree()
    classes = {
        n: c for n in E.__all__ if isinstance(c := getattr(E, n), type) and issubclass(c, E.SalesforceError)
    }
    assert set(tree) == set(classes)
    for name, parent in tree.items():
        expected = classes[name].__mro__[1].__name__ if name != "SalesforceError" else None
        assert parent == expected, f"{name}: page says {parent}, code says {expected}"


def _anchors() -> set[str]:
    """Heading ids as the docs site makes them: lower case, spaces to ``-``, punctuation dropped."""
    out: set[str] = set()
    for line in PAGE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#{2,3} (?P<text>.+)$", line)
        if m:
            text = re.sub(r"[^\w\s-]", "", m.group("text").lower()).strip()
            out.add(re.sub(r"[\s]+", "-", text))
    return out


def test_every_docs_url_has_an_entry_on_the_page() -> None:
    """``SalesforceError.docs_url`` (spec §8.5) must land on a real heading of ``errors.md``."""
    anchors = _anchors()
    missing: list[str] = []
    for name in E.__all__:
        cls = getattr(E, name)
        if not (isinstance(cls, type) and issubclass(cls, E.SalesforceError)):
            continue
        url = cls().docs_url
        assert url is not None
        assert url.startswith(E.ERRORS_PAGE)
        if url.split("#", 1)[1] not in anchors:
            missing.append(name)
    assert not missing, f"add a '### <Class>' entry under '## Troubleshooting' for: {missing}"
    assert "hints-and-docs-links" in anchors
    assert "troubleshooting" in anchors
