"""Run the fenced ``python`` blocks of every shipped ``SKILL.md``, like the docs pages.

Skills are read by coding agents, so their examples must match the real public API. Every
block compiles. Blocks run in order in one namespace per skill, against the synthetic org of
:mod:`tests.docs._org`, with the same rules as :mod:`tests.docs.test_examples`. A block that
needs objects the synthetic org doesn't have (Apex triggers, flows) is opted out with
``<!-- esf: skip -->`` and still compiled. The ``ESF_*`` login variables point at the synthetic
org too.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from ._org import INSTANCE, FakeOrg
from .test_examples import _run_tests, extract
from .test_examples import org as org  # the fixture: re-exported so pytest finds it here

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "src" / "even_simpler_salesforce" / "skills"
PAGES = sorted(SKILLS.glob("*/SKILL.md"))


def test_skills_are_collected() -> None:
    assert len(PAGES) >= 5
    assert all(extract(p) for p in PAGES), "every skill shows code"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.parent.name)
def test_every_block_compiles(page: Path) -> None:
    for block in extract(page):
        compile(block.code, f"{page.parent.name}:{block.line}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def test_most_blocks_run() -> None:
    blocks = [b for p in PAGES for b in extract(p)]
    skipped = [f"{b.path.parent.name}:{b.line}" for b in blocks if b.skipped]
    assert len(skipped) <= 2, skipped


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.parent.name)
def test_skill_examples(
    page: Path, org: FakeOrg, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv("ESF_ACCESS_TOKEN", "AT-docs-static")
    monkeypatch.setenv("ESF_INSTANCE_URL", INSTANCE)
    ns: dict[str, Any] = {"__name__": f"skill_{page.parent.name.replace('-', '_')}"}
    loop = asyncio.new_event_loop()
    try:
        for block in extract(page):
            if block.skipped:
                continue
            where = f"{page.parent.name}/SKILL.md:{block.line}"
            source = "\n" * (block.line - 1) + block.code
            code = compile(source, where, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
            before = set(ns)
            result = eval(code, ns)
            if inspect.isawaitable(result):
                loop.run_until_complete(result)
            _run_tests(ns, before, loop, request)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)


def test_probe_template_compiles() -> None:
    path = SKILLS / "esf-probe" / "probe_template.py"
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
