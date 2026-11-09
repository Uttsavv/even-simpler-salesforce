"""Request specs, raw responses and URL/path helpers. No I/O."""

from __future__ import annotations

import gzip
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote as _urlquote

from ..errors import UnexpectedResponse
from . import jsonlib

__all__ = [
    "GZIP_THRESHOLD",
    "InvalidJSONResponse",
    "RawResponse",
    "RequestSpec",
    "build_url",
    "check_record_id",
    "check_sobject",
    "gzip_body",
    "json_body",
    "path_template",
    "url_segment",
]

GZIP_THRESHOLD = 64 * 1024
_ID_RE = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")
_SOBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ID_SEGMENT = re.compile(r"(?<=/)(?=[a-zA-Z0-9]*\d)[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?(?=/|$)")
_LOCATOR_SEGMENT = re.compile(r"(?<=/)[0-9A-Za-z]{15,18}-\d+(?=/|$)")
_VERSION_PREFIX = re.compile(r"^/services/data/v\d+\.\d")

Params = Mapping[str, Any] | list[tuple[str, Any]]


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """Everything the transport needs to perform one logical Salesforce call.

    Built by the area facades with pure helpers; the transport is the only thing that sends it.

    Attributes:
        method: HTTP method.
        path: Path relative to ``/services/data/vXX.X/``, or absolute starting with
            ``/services/``.
        params: Query-string parameters.
        content: Request body bytes (already encoded).
        content_type: ``Content-Type`` of ``content``.
        headers: Extra headers.
        idempotent: Whether resending is safe. ``None`` infers it from the method
            (everything except POST).
        operation: Operation name for logs, metrics and errors (``"sobject.get"``).
        sobject: sObject type for logs and errors.
        path_template: Path with ids replaced, for logs. Derived when omitted.
        records: Number of records sent, for logs.
        compress: Gzip the body when it's larger than 64 KiB.
        dml: The call writes records; unknown 4xx codes map to :class:`RecordError`.
        soql: SOQL text, for ``soql_hash`` / redacted logging.
        priority: Overrides the caller priority for this call.
        allow_not_modified: Treat 304 as success (conditional GETs).
        expect_json: A non-empty 2xx body must be JSON; otherwise the call fails inside the
            pipeline with ``UnexpectedResponse`` (so the ``sf.request`` event says ``error``).
    """

    method: str
    path: str
    params: Params | None = None
    content: bytes | None = None
    content_type: str | None = None
    headers: Mapping[str, str] = field(default_factory=lambda: {})
    idempotent: bool | None = None
    operation: str = "raw"
    sobject: str | None = None
    path_template: str | None = None
    records: int | None = None
    compress: bool = False
    dml: bool = False
    soql: str | None = None
    priority: str | None = None
    allow_not_modified: bool = False
    expect_json: bool = True

    @property
    def is_idempotent(self) -> bool:
        if self.idempotent is not None:
            return self.idempotent
        return self.method.upper() != "POST"


_UNPARSED: Any = object()


class InvalidJSONResponse(UnexpectedResponse, ValueError):
    """A body that should be JSON isn't (proxy/maintenance page, truncated body).

    An :class:`UnexpectedResponse` for callers, and a ``ValueError`` for parsers that already
    treat undecodable bodies that way.
    """


@dataclass(frozen=True)
class RawResponse:
    """A fully read HTTP response, decoupled from httpx. ``json()`` is parsed once and cached."""

    status: int
    headers: Mapping[str, str]
    content: bytes
    url: str = ""
    loads: Any = field(default=None, repr=False, compare=False)
    _parsed: Any = field(default=_UNPARSED, repr=False, compare=False)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    @property
    def is_json(self) -> bool:
        ct = self.content_type
        return ct == "application/json" or ct.endswith("+json")

    def json(self) -> Any:
        """Decoded JSON body (``None`` when empty).

        Raises:
            InvalidJSONResponse: the body is not valid JSON (an ``UnexpectedResponse`` that is
                also a ``ValueError``).
        """
        if self._parsed is _UNPARSED:
            try:
                value = (self.loads or jsonlib.loads)(self.content) if self.content else None
            except ValueError:
                raise InvalidJSONResponse(
                    f"response body is not valid JSON (HTTP {self.status}, "
                    f"{self.content_type or 'no content-type'})",
                    status=self.status,
                    raw=self.content[:4096],
                ) from None
            object.__setattr__(self, "_parsed", value)
        return self._parsed

    @property
    def request_id(self) -> str | None:
        for name in ("sforce-request-id", "x-request-id", "x-b3-traceid"):
            v = self.headers.get(name)
            if v:
                return v
        return None


def json_body(obj: Any) -> tuple[bytes, str]:
    """Encode ``obj`` as a JSON request body."""
    return jsonlib.dumps(obj), "application/json"


def gzip_body(content: bytes) -> bytes:
    return gzip.compress(content, compresslevel=5)


def url_segment(value: str) -> str:
    """Percent-encode one path segment (``/`` included)."""
    return _urlquote(value, safe="")


def check_sobject(name: str) -> str:
    """Validate an sObject API name before putting it in a path."""
    if not isinstance(name, str) or not _SOBJECT_RE.match(name):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"invalid sObject name: {name!r}")
    return name


def check_record_id(record_id: str) -> str:
    """Validate a 15/18-character Salesforce id before putting it in a path."""
    if not isinstance(record_id, str) or not _ID_RE.match(record_id):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"invalid Salesforce id: {record_id!r}")
    return record_id


def build_url(instance_url: str, api_version: str, path: str) -> str:
    """Absolute URL for ``path``.

    Raises:
        ValueError: absolute URLs, traversal, or anything outside ``/services/``.
    """
    if "://" in path or path.startswith("//"):
        raise ValueError("absolute URLs are not allowed; pass a path under /services/")
    if any(seg in ("..", ".") for seg in path.split("?", 1)[0].split("/")):
        raise ValueError("path traversal is not allowed")
    base = instance_url.rstrip("/")
    if path.startswith("/"):
        if not path.startswith("/services/"):
            raise ValueError("absolute paths must start with /services/")
        return base + path
    return f"{base}/services/data/v{api_version}/{path}"


def path_template(path: str) -> str:
    """Path for logs: version prefix dropped, ids → ``:id``, query locators → ``:locator``.

    Relative (``sobjects/Lead/...``) and absolute (``/services/data/vXX.X/sobjects/Lead/...``)
    spellings of the same resource give the same template, always with a leading ``/``.
    """
    p = path.split("?", 1)[0]
    p = _VERSION_PREFIX.sub("", p)
    if not p.startswith("/"):
        p = "/" + p
    p = _LOCATOR_SEGMENT.sub(":locator", p)
    return _ID_SEGMENT.sub(":id", p)
