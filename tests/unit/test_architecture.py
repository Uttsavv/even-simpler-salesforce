"""Architecture guards, checked on the source text (AST), without importing the modules.

simple-salesforce regressions these rules guard against (behaviour only, no code was read or
copied):

- One HTTP path (#619, #686, #91, #523, #148, #726): requests sent from many places through
  different code paths, so some of them skipped auth refresh, retries, timeouts, proxies or
  session reuse. Here only the transport talks to httpx; everything else builds a
  ``RequestSpec`` and hands it over.
- No hard-coded API versions (#506, #78, #268): paths with a fixed ``vNN.0`` (or a SOAP
  ``Soap/u/NN.0``) that ignored the configured ``api_version`` and broke when Salesforce
  retired that version.
- Pure layers (spec §3 "no I/O"): ``core/``, ``auth/``, ``errors.py`` and ``models.py`` never
  import the I/O layers or httpx, so both clients share them unchanged.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "src" / "even_simpler_salesforce"
FILES = sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)

TRANSPORTS = {"_async/transport.py", "_sync/transport.py"}

# Files (relative to the package) that may import httpx at runtime, and why. Imports under
# ``if TYPE_CHECKING:`` are allowed anywhere and need no entry.
HTTPX_IMPORT_ALLOWLIST: dict[str, str] = {
    # The request pipeline: the only place that builds and sends HTTP requests.
    "_async/transport.py": "the request pipeline (§7.1)",
    # Generated from _async/transport.py by scripts/unasync.py.
    "_sync/transport.py": "generated copy of the request pipeline",
    # RecordingTransport / replay() *are* httpx transports (subclass httpx.BaseTransport and
    # build httpx.Response objects). They forward to a transport the user passes in through
    # ``handle_request``; they never create an httpx client or send a request of their own.
    "testing/recorder.py": "httpx transport implementation for recording/replay",
    # ``esf version`` prints ``httpx.__version__``; the import sits inside that function and
    # every request the CLI makes goes through Salesforce/AsyncSalesforce.
    "cli/__init__.py": "reads httpx.__version__ for `esf version`",
}

# Methods that send a request when called on an httpx client.
SEND_METHODS = frozenset(
    {"send", "request", "stream", "get", "post", "put", "patch", "delete", "head", "options"}
)
# Receiver names the library uses for an httpx client (``self.http``, an injected
# ``http_client``) plus the module itself (``httpx.get(...)``).
HTTP_RECEIVERS = frozenset({"http", "_http", "http_client", "httpx"})
# Within the transport, these are the only (function, method) pairs that hit the network.
TRANSPORT_SENDS = {("post_form", "post"), ("_attempt", "send")}

PURE = ("core/", "auth/", "errors.py", "models.py")
FORBIDDEN_IN_PURE = re.compile(
    r"^(httpx|socket|ssl|http\.client|urllib\.request|asyncio|requests|aiohttp)(\.|$)"
)

VERSION_PATTERNS = [
    re.compile(r"/services/data/v\d"),
    re.compile(r"Soap/[uc]/\d"),
    re.compile(r"(?<![\w.])v?\d{2,3}\.\d(?![\w.])"),
]
# Where an API version literal is allowed: the single source of the default.
VERSION_ALLOWLIST = {("_async/client.py", "DEFAULT_API_VERSION"), ("_sync/client.py", "DEFAULT_API_VERSION")}


def rel(path: Path) -> str:
    return path.relative_to(PKG).as_posix()


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def runtime_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Every node reachable at runtime: skips ``if TYPE_CHECKING:`` bodies (keeps ``else``)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If) and is_type_checking(child.test):
            for stmt in child.orelse:
                yield stmt
                yield from runtime_nodes(stmt)
            continue
        yield child
        yield from runtime_nodes(child)


@dataclass(frozen=True)
class Import:
    module: str  # absolute dotted name, or the resolved package-relative one
    line: int


def resolve(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    # The containing package: drop the module name (or ``__init__``).
    parts = ["even_simpler_salesforce", *path.relative_to(PKG).with_suffix("").parts][:-1]
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def runtime_imports(path: Path) -> list[Import]:
    out: list[Import] = []
    for node in runtime_nodes(parse(path)):
        if isinstance(node, ast.Import):
            out.extend(Import(a.name, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = resolve(path, node)
            out.append(Import(mod, node.lineno))
            # ``from .. import _async`` imports a submodule by name.
            out.extend(Import(f"{mod}.{a.name}", node.lineno) for a in node.names)
    return out


def receiver_name(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def http_calls(path: Path) -> list[tuple[str, str, int]]:
    """``(enclosing function, method, line)`` for every ``<http client>.<send method>(...)``."""
    found: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in SEND_METHODS
                and receiver_name(child.func.value) in HTTP_RECEIVERS
            ):
                found.append((func, child.func.attr, child.lineno))
            visit(child, name)

    visit(parse(path), "<module>")
    return found


def test_files_found() -> None:
    names = {rel(p) for p in FILES}
    assert names >= TRANSPORTS
    assert set(HTTPX_IMPORT_ALLOWLIST) <= names, "stale allowlist entry"
    assert len(FILES) > 30


# --- one HTTP path ----------------------------------------------------------------------------


@pytest.mark.parametrize("path", FILES, ids=rel)
def test_only_allowlisted_files_import_httpx(path: Path) -> None:
    hits = [i for i in runtime_imports(path) if i.module == "httpx" or i.module.startswith("httpx.")]
    if rel(path) in HTTPX_IMPORT_ALLOWLIST:
        return
    assert not hits, (
        f"{rel(path)} imports httpx at runtime (lines {[i.line for i in hits]}); go through the transport"
    )


def test_allowlisted_files_really_need_the_entry() -> None:
    """Keep the allowlist tight: drop an entry once the file no longer imports httpx."""
    for name in HTTPX_IMPORT_ALLOWLIST:
        mods = {i.module for i in runtime_imports(PKG / name)}
        assert "httpx" in mods, f"{name} no longer imports httpx; remove it from the allowlist"


@pytest.mark.parametrize("path", FILES, ids=rel)
def test_only_the_transport_sends_requests(path: Path) -> None:
    calls = http_calls(path)
    if rel(path) in TRANSPORTS:
        assert {(f, m) for f, m, _ in calls} == TRANSPORT_SENDS
        return
    assert not calls, f"{rel(path)} sends HTTP requests directly: {calls}"


@pytest.mark.parametrize("name", sorted(set(HTTPX_IMPORT_ALLOWLIST) - TRANSPORTS))
def test_non_transport_allowlisted_files_never_create_clients(name: str) -> None:
    tree = parse(PKG / name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func
            if receiver_name(target.value) == "httpx":
                assert target.attr not in {"Client", "AsyncClient", *SEND_METHODS}, (
                    f"{name}:{node.lineno} creates an httpx client or sends a request"
                )


# --- no hard-coded API versions ------------------------------------------------------------------


def docstring_nodes(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def allowed_constants(path: Path, tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if (
                    isinstance(t, ast.Name)
                    and (rel(path), t.id) in VERSION_ALLOWLIST
                    and node.value is not None
                ):
                    ids.update(id(c) for c in ast.walk(node.value))
    return ids


def version_literals(path: Path) -> list[tuple[int, str]]:
    tree = parse(path)
    skip = docstring_nodes(tree) | allowed_constants(path, tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and any(p.search(node.value) for p in VERSION_PATTERNS)
        ):
            out.append((node.lineno, node.value))
    return out


@pytest.mark.parametrize("path", FILES, ids=rel)
def test_no_hard_coded_api_versions(path: Path) -> None:
    assert version_literals(path) == [], "use DEFAULT_API_VERSION / the configured api_version"


def test_default_api_version_is_defined_once_per_flavour() -> None:
    defs = [
        rel(p)
        for p in FILES
        for node in ast.walk(parse(p))
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "DEFAULT_API_VERSION" for t in node.targets)
    ]
    assert sorted(defs) == ["_async/client.py", "_sync/client.py"]


@pytest.mark.parametrize(
    "text",
    ["/services/data/v59.0/query", "Soap/u/61.0", "v62.0", "68.0", "x 60.0 y"],
)
def test_version_patterns_catch_examples(text: str) -> None:
    assert any(p.search(text) for p in VERSION_PATTERNS)


@pytest.mark.parametrize("text", ["3.10", "1.5", "100.0.1", "0.25", "py3.12", "HTTP 404"])
def test_version_patterns_ignore_other_numbers(text: str) -> None:
    assert not any(p.search(text) for p in VERSION_PATTERNS)


# --- pure layers ------------------------------------------------------------------------------------

PURE_FILES = [p for p in FILES if rel(p).startswith(PURE)]


def test_pure_files_found() -> None:
    names = {rel(p) for p in PURE_FILES}
    assert {"errors.py", "models.py", "core/classify.py", "auth/flows.py"} <= names


@pytest.mark.parametrize("path", PURE_FILES, ids=rel)
def test_pure_layers_import_no_io(path: Path) -> None:
    bad = [
        (i.line, i.module)
        for i in runtime_imports(path)
        if FORBIDDEN_IN_PURE.match(i.module)
        or i.module.startswith(("even_simpler_salesforce._async", "even_simpler_salesforce._sync"))
    ]
    assert bad == [], f"{rel(path)} must stay free of I/O (spec §3)"


def test_resolve_relative_imports() -> None:
    tree = ast.parse("from .._async import client\nfrom . import jsonlib\nfrom ...x import y\n")
    nodes = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    path = PKG / "core" / "http.py"
    assert resolve(path, nodes[0]) == "even_simpler_salesforce._async"
    assert resolve(path, nodes[1]) == "even_simpler_salesforce.core"
    assert resolve(PKG / "core" / "__init__.py", nodes[1]) == "even_simpler_salesforce.core"


# --- the Pub/Sub extra (§5.1, D31) -----------------------------------------------------------------

# The [pubsub] extra's packages, and the only files that may import each at runtime. Everything
# else, in particular the client modules every user imports, must work without the extra.
PUBSUB_IMPORT_ALLOWLIST: dict[str, set[str]] = {
    # gRPC calls go through the GrpcStream adapter only (the generated stub module is unused).
    "grpc": {"_async/_compat.py", "_sync/_compat.py", "_pubsub/pubsub_api_pb2_grpc.py"},
    # The protobuf messages: the generated stubs and the gRPC path that builds requests.
    "google.protobuf": {"_pubsub/pubsub_api_pb2.py"},
    "even_simpler_salesforce._pubsub": {
        "_async/pubsub_transport.py",
        "_sync/pubsub_transport.py",
        "_pubsub/pubsub_api_pb2_grpc.py",
    },
    # Avro decoding is a pure function in core/pubsub.py.
    "fastavro": {"core/pubsub.py"},
}


@pytest.mark.parametrize("package", sorted(PUBSUB_IMPORT_ALLOWLIST))
def test_pubsub_extra_imports_stay_in_the_pubsub_modules(package: str) -> None:
    importers = {
        rel(p)
        for p in FILES
        if any(i.module == package or i.module.startswith(package + ".") for i in runtime_imports(p))
    }
    assert importers == PUBSUB_IMPORT_ALLOWLIST[package], f"only these files may import {package}"


def test_client_modules_import_the_pubsub_transport_lazily() -> None:
    """``import even_simpler_salesforce`` must work without the [pubsub] extra."""
    for name in ("_async/client.py", "_async/api/pubsub.py", "__init__.py"):
        mods = {i.module for i in runtime_imports(PKG / name) if not i.module.endswith("pubsub_transport")}
        assert not {m for m in mods if m.startswith(("grpc", "google.protobuf", "fastavro"))}, name
