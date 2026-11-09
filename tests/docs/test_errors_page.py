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
