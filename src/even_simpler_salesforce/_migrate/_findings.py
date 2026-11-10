"""Findings, markers and the JSON report for ``esf migrate`` (§9.19.3, §9.19.4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "DOCS_BASE",
    "MARKER_RE",
    "RULES",
    "Finding",
    "FileResult",
    "Status",
    "marker_text",
    "redact",
]

Status = Literal["rewritten", "needs-review", "todo"]

#: Rule ids are public (docs link to ``.../migrate/#<rule>``): add new ones, never rename.
RULES: tuple[str, ...] = (
    "sobject-attr",
    "sobject-getattr",
    "upsert-path",
    "get-by-custom-id",
    "create-return",
    "return-value",
    "stored-ref",
    "mock-sobject",
    "query-all",
    "query-result",
    "query-more",
    "format-soql",
    "restful",
    "apexecute",
    "exceptions",
    "client-internals",
    "client-methods",
    "login",
    "not-a-client",
    "unsupported",
)

DOCS_BASE = "https://uttsavv.github.io/even-simpler-salesforce/latest/migrate/"

#: ``# esf-migrate: TODO[rule] message`` / ``REVIEW[rule]`` / ``ignore[rule]``.
MARKER_RE = re.compile(r"#\s*esf-migrate:\s*(?P<kind>TODO|REVIEW|ignore)\[(?P<rule>[a-z-]+)\]")

# Any string literal (optional prefix, triple or single quotes, possibly multi-line).
_STRING = re.compile(
    r"""(?P<p>(?:(?<![A-Za-z0-9_])[rRbBuUfF]{1,2})?)(?P<q>\"\"\"|\'\'\'|"|')(?P<body>(?:\\.|(?!(?P=q)).)*?)(?P=q)""",
    re.DOTALL,
)
_REDACTED_NAMES = [  # names whose values are redacted
    "password",
    "passwd",
    "secret",
    "client_secret",
    "token",
    "access_token",
    "refresh_token",
    "security_token",
    "session_id",
    "private_?key",
    "api_?key",
    "key",
]
_SECRET_NAMES = "(?:" + "|".join(_REDACTED_NAMES) + ")"
_SECRETS = (
    re.compile(r"00D[A-Za-z0-9]{12,15}![A-Za-z0-9._\-]+"),  # Salesforce session id / access token
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-!]{16,}"),
    # name="value" / name='value' (keyword arguments, assignments)
    re.compile(r"(?i)\b(" + _SECRET_NAMES + r")(\s*=\s*)(['\"])[^'\"]*\3"),
    # "name": "value" (dict literals, JSON)
    re.compile(r"(?i)(['\"]" + _SECRET_NAMES + r"['\"]\s*:\s*)(['\"])[^'\"]*\2"),
)


def _shorten(m: re.Match[str]) -> str:
    body = m.group("body")
    if len(body) <= 80:
        return m.group(0)
    return f"{m.group('p')}{m.group('q')}…{m.group('q')}"


def redact(code: str) -> str:
    """Drop string literals longer than 80 characters and anything that looks like a secret."""
    out = _SECRETS[0].sub("<redacted>", code)
    out = _SECRETS[1].sub(lambda m: f"{m.group(1)} <redacted>", out)
    out = _SECRETS[2].sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<redacted>{m.group(3)}", out)
    out = _SECRETS[3].sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>{m.group(2)}", out)
    return _STRING.sub(_shorten, out)


def marker_text(status: Status, rule: str, message: str) -> str:
    kind = "TODO" if status == "todo" else "REVIEW"
    return f"# esf-migrate: {kind}[{rule}] {message}"


@dataclass
class Finding:
    """One site found by a rule."""

    rule: str
    status: Status
    message: str
    line: int
    col: int
    end_line: int
    end_col: int
    before: str
    after: str | None = None
    suggestion: str | None = None
    file: str = ""
    holder_line: int = 0  # start line of the statement that carries the marker
    markers_added: int = 0  # markers this run inserted above that statement

    def as_json(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "rule": self.rule,
            "status": self.status,
            "before": redact(self.before),
            "after": redact(self.after) if self.after is not None else None,
            "message": self.message,
            "suggestion": redact(self.suggestion) if self.suggestion is not None else None,
            "docs_url": DOCS_BASE + "#" + self.rule,
        }


@dataclass
class FileResult:
    path: str
    original: str
    updated: str
    findings: list[Finding] = field(default_factory=lambda: [])
    error: str | None = None
    encoding: str = "utf-8"  # PEP 263 encoding of the file; "utf-8-sig" keeps a BOM

    @property
    def changed(self) -> bool:
        return self.updated != self.original
