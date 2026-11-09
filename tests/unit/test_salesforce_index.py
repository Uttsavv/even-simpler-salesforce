"""Keep ``docs/salesforce-dependencies.md`` in step with the code.

The page is the one place to compare against each Salesforce release. These tests read the
source (AST, no imports of the I/O layers) and fail when the code depends on an endpoint,
OAuth endpoint, grant type, ``Sforce-*`` header or error code that the page doesn't list, or
when the page lists an endpoint the code no longer uses.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from even_simpler_salesforce.core.classify import CODE_MAP
from even_simpler_salesforce.core.records import COLLECTION_READ_CHUNK, COLLECTION_WRITE_CHUNK

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "even_simpler_salesforce"
INDEX = ROOT / "docs" / "salesforce-dependencies.md"
ASYNC_FILES = sorted((PKG / "_async").rglob("*.py"))
SOURCE_FILES = sorted(p for p in PKG.rglob("*.py") if "_sync" not in p.parts and "testing" not in p.parts)

# Index entries the code reaches through a runtime value rather than a literal path.
DYNAMIC_PATHS = {
    "query/{}",  # nextRecordsUrl, carried inside the opaque cursor
}


def index_text() -> str:
    return INDEX.read_text(encoding="utf-8")


def index_keys() -> set[str]:
    """Backticked first cells of every table row."""
    return {m.group(1) for m in re.finditer(r"^\| `([^`]+)`", index_text(), re.MULTILINE)}


def normalise(node: ast.expr) -> list[str]:
    """Literal path(s) of an expression, with interpolated parts as ``{}``; [] when dynamic."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            "".join(
                str(v.value) if isinstance(v, ast.Constant) else "{}"
                for v in node.values  # pyright: ignore[reportUnknownVariableType]
            )
        ]
    if isinstance(node, ast.IfExp):
        return normalise(node.body) + normalise(node.orelse)
    return []


def code_paths() -> set[str]:
    """Paths passed to ``RequestSpec``/``_write(method, path)`` and ``_describe(path)`` in ``_async/``."""
    out: set[str] = set()
    for path in ASYNC_FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in {"RequestSpec", "_write"} and len(node.args) >= 2:
                out.update(normalise(node.args[1]))
            elif name == "_describe" and node.args:
                out.update(normalise(node.args[0]))
    return out


def as_pattern(key: str) -> re.Pattern[str]:
    return re.compile("^" + re.escape(key).replace(r"\{\}", "[^/]+") + "$")


def matches(a: str, b: str) -> bool:
    """``{}`` in either key matches one path segment of the other."""
    return a == b or bool(as_pattern(a).match(b)) or bool(as_pattern(b).match(a))


def string_constants(paths: list[Path]) -> set[str]:
    out: set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.add(node.value)
    return out


def rest_keys() -> set[str]:
    section = index_text().split("## REST endpoints", 1)[1].split("\n## ", 1)[0]
    return {m.group(1) for m in re.finditer(r"^\| `([^`]+)`", section, re.MULTILINE)}


def test_index_exists_and_has_keys() -> None:
    assert INDEX.is_file()
    assert len(index_keys()) > 50


def test_every_endpoint_in_code_is_indexed() -> None:
    paths = code_paths()
    assert {"query", "limits", "composite/sobjects"} <= paths  # the scan works
    keys = index_keys()
    missing = sorted(p for p in paths if not any(matches(p, k) for k in keys))
    assert not missing, f"add these endpoints to {INDEX.name}: {missing}"


def test_every_indexed_endpoint_is_used() -> None:
    paths = code_paths()
    stale = sorted(
        k
        for k in rest_keys() - DYNAMIC_PATHS
        if not k.startswith("/") and not any(matches(k, p) for p in paths)
    )
    assert not stale, f"{INDEX.name} lists endpoints the code no longer calls: {stale}"


def test_every_oauth_endpoint_is_indexed() -> None:
    used = {
        m.group(0) for s in string_constants(SOURCE_FILES) for m in re.finditer(r"/services/oauth2/[a-z]+", s)
    }
    assert {"/services/oauth2/token", "/services/oauth2/authorize"} <= used
    assert used <= index_keys(), f"add to {INDEX.name}: {sorted(used - index_keys())}"


def test_every_grant_type_is_indexed() -> None:
    tree = ast.parse((PKG / "auth" / "flows.py").read_text(encoding="utf-8"))
    grants = {
        v.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for k, v in zip(node.keys, node.values)
        if isinstance(k, ast.Constant) and k.value == "grant_type" and isinstance(v, ast.Constant)
    }
    assert {"client_credentials", "refresh_token", "authorization_code"} <= grants
    assert grants <= index_keys(), f"add to {INDEX.name}: {sorted(grants - index_keys())}"


def test_every_sforce_header_is_indexed() -> None:
    used = {s.lower() for s in string_constants(SOURCE_FILES) if re.fullmatch(r"(?i)sforce-[a-z-]+", s)}
    assert {"sforce-call-options", "sforce-limit-info"} <= used
    indexed = {k.lower() for k in index_keys()}
    assert used <= indexed, f"add to {INDEX.name}: {sorted(used - indexed)}"


def test_error_codes_match_code_map() -> None:
    section = index_text().split("## Error codes", 1)[1].split("\n## ", 1)[0]
    listed = {m.group(1) for m in re.finditer(r"^\| `([A-Z_]+)` \|", section, re.MULTILINE)}
    assert listed == set(CODE_MAP)
    for code, cls in CODE_MAP.items():
        assert re.search(rf"^\| `{code}` \| `{cls.__name__}`", section, re.MULTILINE), code


def test_versions_and_sizes_match_the_constants() -> None:
    tree = ast.parse((PKG / "_async" / "client.py").read_text(encoding="utf-8"))
    consts = {
        t.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id in {"DEFAULT_API_VERSION", "MIN_API_VERSION"}
    }
    text = index_text()
    assert f"| Default API version | `{consts['DEFAULT_API_VERSION']}` |" in text
    minimum = ".".join(str(x) for x in consts["MIN_API_VERSION"])
    assert f"| Oldest supported version | `{minimum}` |" in text
    assert f"| Collections write chunk | {COLLECTION_WRITE_CHUNK} records |" in text
    assert f"| Collections retrieve chunk | {COLLECTION_READ_CHUNK:,} ids |" in text


def test_matches_helper() -> None:
    assert matches("sobjects/{}/{}/", "sobjects/{}/updated/")
    assert matches("sobjects/{}", "sobjects/{}")
    assert not matches("sobjects/{}", "sobjects/{}/describe")
