"""The framework-neutral error mapping shared by the FastAPI and Flask integrations (§13.1),
and the rule that framework imports stay inside ``contrib/`` (O7: the core depends on httpx only).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.contrib._http import DEFAULT_RETRY_AFTER, HANDLED, http_error

PKG = Path(esf.__file__).resolve().parent
FRAMEWORKS = ("fastapi", "flask", "starlette", "werkzeug")


def test_reauth_is_401_with_stable_code() -> None:
    mapped = http_error(esf.ReauthorizationRequired("refresh token revoked", error_code="invalid_grant"))
    assert mapped is not None
    assert (mapped.status, mapped.body, mapped.headers) == (401, {"code": "salesforce_reauth_required"}, {})


def test_record_error_is_422_with_details() -> None:
    exc = esf.ValidationRuleFailed(
        "Email is invalid", error_code="FIELD_CUSTOM_VALIDATION_EXCEPTION", fields=("Email",)
    )
    mapped = http_error(exc)
    assert mapped is not None
    assert mapped.status == 422
    assert mapped.body == {
        "code": "salesforce_record_error",
        "error_code": "FIELD_CUSTOM_VALIDATION_EXCEPTION",
        "message": "Email is invalid",
        "fields": ["Email"],
    }


def test_not_found_is_404() -> None:
    mapped = http_error(esf.NotFound("gone", error_code="NOT_FOUND"))
    assert mapped is not None
    assert (mapped.status, mapped.body) == (404, {"code": "salesforce_not_found", "error_code": "NOT_FOUND"})


def test_invalid_cursor_is_400_but_bad_soql_is_unmapped() -> None:
    mapped = http_error(esf.MalformedQuery("invalid cursor", error_code="INVALID_CURSOR"))
    assert mapped is not None
    assert mapped.status == 400
    assert mapped.body["code"] == "salesforce_invalid_cursor"
    assert http_error(esf.MalformedQuery("unexpected token", error_code="MALFORMED_QUERY")) is None


@pytest.mark.parametrize(
    ("exc", "retry_after"),
    [
        (esf.RequestLimitExceeded("busy", error_code="REQUEST_LIMIT_EXCEEDED"), str(DEFAULT_RETRY_AFTER)),
        (esf.RequestLimitExceeded("busy", retry_after=2.2), "3"),
        (esf.RequestLimitExceeded("busy", retry_after=0), "1"),
        (esf.BudgetExceeded("budget"), str(DEFAULT_RETRY_AFTER)),
    ],
)
def test_limits_are_503_with_retry_after(exc: esf.LimitError, retry_after: str) -> None:
    mapped = http_error(exc)
    assert mapped is not None
    assert mapped.status == 503
    assert mapped.body["code"] == "salesforce_limit"
    assert mapped.headers == {"Retry-After": retry_after}


def test_transport_is_502_and_server_is_503() -> None:
    transport = http_error(esf.RequestTimeout("timed out"))
    server = http_error(esf.ServerError("unavailable", error_code="SERVER_UNAVAILABLE"))
    assert transport is not None
    assert server is not None
    assert (transport.status, transport.body) == (502, {"code": "salesforce_unavailable"})
    assert (server.status, server.body["code"]) == (503, "salesforce_unavailable")


def test_event_publish_unavailable_is_503_not_422() -> None:
    """Publishing down for a moment is an outage, not a bad record (it subclasses RecordError)."""
    err = http_error(esf.EventPublishUnavailable("down", error_code="PLATFORM_EVENT_PUBLISHING_UNAVAILABLE"))
    assert err is not None
    assert err.status == 503
    assert err.body == {
        "code": "salesforce_unavailable",
        "error_code": "PLATFORM_EVENT_PUBLISHING_UNAVAILABLE",
    }
    failed = http_error(esf.EventPublishFailed("over", error_code="LIMIT_EXCEEDED"))
    assert failed is not None and failed.status == 422


@pytest.mark.parametrize(
    "exc", [esf.InvalidField("x"), esf.SessionExpired("x"), esf.UnexpectedResponse("x"), ValueError("x")]
)
def test_other_errors_are_left_to_the_framework(exc: Exception) -> None:
    assert http_error(exc) is None


def test_every_mapped_class_is_registered() -> None:
    samples = [
        esf.ReauthorizationRequired(),
        esf.RowLocked(),
        esf.NotFound(),
        esf.MalformedQuery(error_code="INVALID_CURSOR"),
        esf.BudgetExceeded(),
        esf.ConnectError(),
        esf.ServerError(),
    ]
    for exc in samples:
        assert http_error(exc) is not None
        assert isinstance(exc, HANDLED)


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize(
    "path",
    [p for p in sorted(PKG.rglob("*.py")) if "contrib" not in p.relative_to(PKG).parts],
    ids=lambda p: p.relative_to(PKG).as_posix(),
)
def test_frameworks_are_imported_only_in_contrib(path: Path) -> None:
    assert not _imports(path) & set(FRAMEWORKS)


def test_importing_the_package_loads_no_framework() -> None:
    code = (
        "import sys, even_simpler_salesforce, even_simpler_salesforce.contrib, "
        "even_simpler_salesforce.testing, even_simpler_salesforce.observability;"
        f"print(sorted(m for m in {FRAMEWORKS!r} if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
