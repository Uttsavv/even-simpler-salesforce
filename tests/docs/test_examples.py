"""Run every fenced ``python`` block in ``docs/**/*.md`` and ``README.md`` (spec §15).

Rules for writers of documentation pages:

- Blocks on one page run **in order, in one namespace**, like a doctest session, so a later
  block may use names an earlier block defined.
- Top-level ``await`` / ``async with`` / ``async for`` are allowed (the block is compiled with
  ``PyCF_ALLOW_TOP_LEVEL_AWAIT`` and run on the page's event loop), and so is
  ``asyncio.run(main())``.
- Every HTTP request goes to a synthetic org (:mod:`tests.docs._org`) through respx, whatever
  the host. The environment has ``SF_DOMAIN``, ``SF_CLIENT_ID``, ``SF_CLIENT_SECRET``,
  ``SF_USERNAME``, ``SF_ACCESS_TOKEN`` and ``SF_INSTANCE_URL`` set, and the working directory
  contains ``server.key`` (an RSA private key) and ``server-encrypted.key`` (the same key,
  encrypted with ``SF_KEY_PASSPHRASE``) for JWT examples.
- Functions named ``test_*`` defined by a block are called afterwards with pytest-style fixture
  arguments (``esf_fake`` / ``esf_async_fake`` get fresh fakes, ``esf_replay`` resolves paths
  against the working directory; other names are resolved with
  ``request.getfixturevalue``). Coroutine functions are awaited.
- ``Test*`` classes that subclass ``TokenStoreContract`` / ``AsyncTokenStoreContract`` are run
  with pytest in a subprocess, from a module holding the imports and definitions of every block
  of the page up to that point.
- A block that can't run (needs a database, a real org, ...) is opted out by putting
  ``<!-- esf: skip -->`` on the line right before its opening fence. Keep those rare.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import respx

from ._org import INSTANCE, FakeOrg

ROOT = Path(__file__).resolve().parents[2]
SKIP_MARK = "<!-- esf: skip -->"
_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})\s*(?P<lang>[\w+-]*)(?P<rest>.*)$")


@dataclass
class Block:
    path: Path
    line: int  # 1-based line of the first code line
    code: str
    skipped: bool


def pages() -> list[Path]:
    return [*sorted((ROOT / "docs").rglob("*.md")), ROOT / "README.md"]


def extract(path: Path) -> list[Block]:
    """Fenced code blocks tagged ``python``/``py``, dedented (blocks inside tabs/admonitions)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        m = _OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        indent, fence, lang = m.group("indent"), m.group("fence"), m.group("lang").lower()
        prev = next((lines[j].strip() for j in range(i - 1, -1, -1) if lines[j].strip()), "")
        body: list[str] = []
        j = i + 1
        while j < len(lines) and not (
            lines[j].strip().startswith(fence) and lines[j].strip().strip(fence[0]) == ""
        ):
            body.append(lines[j])
            j += 1
        if lang in ("python", "py"):
            code = textwrap.dedent("\n".join(body)) if indent == "" else _unindent(body, indent)
            blocks.append(Block(path, i + 2, code, prev == SKIP_MARK))
        i = j + 1
    return blocks


def _unindent(body: list[str], indent: str) -> str:
    return "\n".join(line[len(indent) :] if line.startswith(indent) else line.lstrip() for line in body)


PAGES = [p for p in pages() if extract(p)]


def test_every_page_with_python_blocks_is_collected() -> None:
    assert ROOT / "README.md" in PAGES
    assert any(p.parent.name == "guides" for p in PAGES)


def test_skips_are_rare() -> None:
    blocks = [b for p in pages() for b in extract(p)]
    skipped = [f"{b.path.relative_to(ROOT)}:{b.line}" for b in blocks if b.skipped]
    assert len(skipped) <= max(3, len(blocks) // 10), skipped


@pytest.fixture
def org(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "server.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (tmp_path / "server-encrypted.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"docs-key-passphrase"),
        )
    )
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "SF_DOMAIN": INSTANCE,
        "SF_CLIENT_ID": "3MVG9docs.client.id",
        "SF_CLIENT_SECRET": "docs-client-secret",
        "SF_USERNAME": "integration@acme.example",
        "SF_ACCESS_TOKEN": "AT-docs-static",
        "SF_INSTANCE_URL": INSTANCE,
        "SF_KEY_PASSPHRASE": "docs-key-passphrase",
        "ESF_SANDBOX_ACCESS_TOKEN": "AT-docs-sandbox",  # esf_sandbox examples hit the fake org
        "ESF_SANDBOX_INSTANCE_URL": INSTANCE,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ESF_DEBUG", raising=False)
    fake = FakeOrg()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route().mock(side_effect=fake.handle)
        yield fake


def _run_tests(ns: dict[str, Any], before: set[str], loop: asyncio.AbstractEventLoop, request: Any) -> None:
    from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce, replay

    for name, fn in list(ns.items()):
        if name in before or not name.startswith("test_") or not inspect.isfunction(fn):
            continue
        kwargs: dict[str, Any] = {}
        for param in inspect.signature(fn).parameters:
            if param == "esf_fake":
                kwargs[param] = FakeSalesforce()
            elif param == "esf_async_fake":
                kwargs[param] = AsyncFakeSalesforce()
            elif param == "esf_replay":  # relative paths resolve against the page's directory
                kwargs[param] = lambda path, **kw: replay(Path(path).resolve(), **kw)
            else:
                kwargs[param] = request.getfixturevalue(param)
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            loop.run_until_complete(result)


def _run_contracts(ns: dict[str, Any], before: set[str], sources: list[str], tmp: Path, where: str) -> None:
    from even_simpler_salesforce.testing import AsyncTokenStoreContract, TokenStoreContract

    new = [
        v
        for k, v in ns.items()
        if k not in before
        and k.startswith("Test")
        and isinstance(v, type)
        and issubclass(v, (TokenStoreContract, AsyncTokenStoreContract))
    ]
    if not new:
        return
    module = tmp / f"test_docs_contract_{len(sources)}.py"
    keep = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    defs = [
        ast.unparse(node)
        for src in sources
        for node in ast.parse(src).body  # only imports and definitions: no calls to the org
        if isinstance(node, keep)
    ]
    module.write_text("\n\n".join(defs) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", str(module)],
        cwd=tmp,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"{where}: contract suite failed\n{proc.stdout}\n{proc.stderr}"
    assert " skipped" not in proc.stdout, f"{where}: contract tests were skipped\n{proc.stdout}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_page_examples(page: Path, org: FakeOrg, request: pytest.FixtureRequest) -> None:
    ns: dict[str, Any] = {"__name__": f"docs_example_{page.stem.replace('-', '_')}"}
    loop = asyncio.new_event_loop()
    sources: list[str] = []
    try:
        for block in extract(page):
            if block.skipped:
                continue
            where = f"{page.relative_to(ROOT)}:{block.line}"
            source = "\n" * (block.line - 1) + block.code  # tracebacks point at the page line
            code = compile(source, where, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
            before = set(ns)
            result = eval(code, ns)
            if inspect.isawaitable(result):
                loop.run_until_complete(result)
            sources.append(block.code)
            _run_tests(ns, before, loop, request)
            _run_contracts(ns, before, sources, Path.cwd(), where)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)
