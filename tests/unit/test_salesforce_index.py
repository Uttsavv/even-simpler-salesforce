"""Keep ``docs/salesforce-dependencies.yaml`` (and the page generated from it) in step with the code.

The YAML file is the one place to compare against each Salesforce release. These tests read the
source with ``ast`` (no I/O layers imported) and fail when the code depends on an endpoint,
OAuth endpoint, grant type, ``Sforce-*`` header, error code, HTTP status or size that the index
doesn't list, or when the index lists an endpoint the code no longer calls.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path
from typing import Any

import yaml

from even_simpler_salesforce.core.classify import _RETRYABLE_STATUS, _STATUS_MAP, CODE_MAP
from even_simpler_salesforce.core.http import GZIP_THRESHOLD
from even_simpler_salesforce.core.records import COLLECTION_READ_CHUNK, COLLECTION_WRITE_CHUNK

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "even_simpler_salesforce"
INDEX = ROOT / "docs" / "salesforce-dependencies.yaml"
ASYNC_FILES = sorted((PKG / "_async").rglob("*.py"))
SOURCE_FILES = sorted(p for p in PKG.rglob("*.py") if "_sync" not in p.parts and "testing" not in p.parts)
REQUIRED_KEYS = {"path", "methods", "used_by", "docs", "checked"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def index() -> dict[str, Any]:
    return yaml.safe_load(INDEX.read_text(encoding="utf-8"))


def all_paths(data: dict[str, Any]) -> set[str]:
    return {e["path"] for e in data["rest_endpoints"]} | {e["path"] for e in data["oauth"]["endpoints"]}


def normalise(node: ast.expr) -> list[str]:
    """Literal path(s) of an expression, interpolated parts as ``{}``; [] when fully dynamic."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return ["".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)]
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
    return {
        node.value
        for path in paths
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


# --- the file itself ---------------------------------------------------------------------------


def test_index_is_well_formed() -> None:
    data = index()
    entries = [*data["rest_endpoints"], *data["oauth"]["endpoints"]]
    for e in entries:
        assert set(e) - {"dynamic"} == REQUIRED_KEYS, e
        assert e["methods"]
        assert all(m in {"GET", "POST", "PATCH", "PUT", "DELETE", "HEAD"} for m in e["methods"])
    paths = [e["path"] for e in entries]
    assert len(paths) == len(set(paths)), "duplicate paths"
    dated = [*entries, *data["oauth"]["grant_types"], *data["headers"]]
    for e in dated:
        assert e["checked"] is None or DATE.match(str(e["checked"])), e
        assert e["docs"] is None or str(e["docs"]).startswith("https://"), e
    review = data["last_full_review"]
    assert review is None or DATE.match(str(review))


def test_generated_page_is_current() -> None:
    spec = importlib.util.spec_from_file_location("render_sf_index", ROOT / "scripts" / "render_sf_index.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--check"]) == 0, "run: python scripts/render_sf_index.py"


# --- code ↔ index -----------------------------------------------------------------------------------


def test_every_endpoint_in_code_is_indexed() -> None:
    paths = code_paths()
    assert {"query", "limits", "composite/sobjects"} <= paths  # the scan works
    keys = all_paths(index())
    missing = sorted(p for p in paths if not any(matches(p, k) for k in keys))
    assert not missing, f"add these endpoints to {INDEX.name}: {missing}"


def test_every_indexed_endpoint_is_used() -> None:
    paths = code_paths()
    stale = sorted(
        e["path"]
        for e in index()["rest_endpoints"]
        if not e.get("dynamic")
        and not e["path"].startswith("/")
        and not any(matches(e["path"], p) for p in paths)
    )
    assert not stale, f"{INDEX.name} lists endpoints the code no longer calls: {stale}"


def test_every_oauth_endpoint_is_indexed() -> None:
    used = {
        m.group(0) for s in string_constants(SOURCE_FILES) for m in re.finditer(r"/services/oauth2/[a-z]+", s)
    }
    assert {"/services/oauth2/token", "/services/oauth2/authorize"} <= used
    indexed = all_paths(index())
    assert used <= indexed, f"add to {INDEX.name}: {sorted(used - indexed)}"


def test_grant_types_match() -> None:
    tree = ast.parse((PKG / "auth" / "flows.py").read_text(encoding="utf-8"))
    grants = {
        v.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for k, v in zip(node.keys, node.values)
        if isinstance(k, ast.Constant) and k.value == "grant_type" and isinstance(v, ast.Constant)
    }
    assert {"client_credentials", "refresh_token", "authorization_code"} <= grants
    assert grants == {g["grant_type"] for g in index()["oauth"]["grant_types"]}


def test_every_sforce_header_is_indexed() -> None:
    used = {s.lower() for s in string_constants(SOURCE_FILES) if re.fullmatch(r"(?i)sforce-[a-z-]+", s)}
    assert {"sforce-call-options", "sforce-limit-info"} <= used
    indexed = {h["name"].lower() for h in index()["headers"]}
    assert used <= indexed, f"add to {INDEX.name}: {sorted(used - indexed)}"


def test_error_codes_match_code_map() -> None:
    assert index()["error_codes"] == {code: cls.__name__ for code, cls in CODE_MAP.items()}


def test_http_statuses_cover_the_status_map() -> None:
    expected = set(_STATUS_MAP) | set(_RETRYABLE_STATUS)
    listed = set(index()["http_statuses"])
    assert expected <= listed, f"add to {INDEX.name}: {sorted(expected - listed)}"


def test_versions_and_sizes_match_the_constants() -> None:
    tree = ast.parse((PKG / "_async" / "client.py").read_text(encoding="utf-8"))
    consts = {
        t.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id in {"DEFAULT_API_VERSION", "MIN_API_VERSION"}
    }
    data = index()
    assert data["api_versions"]["default"] == consts["DEFAULT_API_VERSION"]
    assert data["api_versions"]["minimum"] == ".".join(str(x) for x in consts["MIN_API_VERSION"])
    limits = data["limits"]
    assert limits["collection_write_chunk"] == COLLECTION_WRITE_CHUNK
    assert limits["collection_read_chunk"] == COLLECTION_READ_CHUNK
    assert limits["gzip_threshold_bytes"] == GZIP_THRESHOLD


def test_matches_helper() -> None:
    assert matches("sobjects/{}/{}/", "sobjects/{}/updated/")
    assert matches("sobjects/{}", "sobjects/{}")
    assert not matches("sobjects/{}", "sobjects/{}/describe")
