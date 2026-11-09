"""Record real Salesforce traffic into redacted JSON fixtures, and replay it (§14).

Recording::

    transport = esf.testing.record("tests/fixtures/leads.json")
    sf = esf.Salesforce(auth, http_client=httpx.Client(transport=transport))
    ...                                    # talk to a real org; each exchange is appended

Replaying::

    sf = esf.Salesforce(
        esf.StaticToken("x", "https://example.my.salesforce.com"),
        http_client=httpx.Client(transport=esf.testing.replay("tests/fixtures/leads.json")),
    )

The same transport object works with ``httpx.Client`` and ``httpx.AsyncClient``.

Fixture format (version 1)
--------------------------

A UTF-8 JSON document::

    {
      "esf_fixture": 1,
      "interactions": [
        {
          "request": {
            "method": "GET",
            "path": "/services/data/v68.0/query",
            "query": [["q", "SELECT Id FROM Lead"]],
            "headers": {"accept": "application/json"},
            "json": null
          },
          "response": {
            "status": 200,
            "headers": {"content-type": "application/json", "sforce-limit-info": "api-usage=1/15000"},
            "json": {"totalSize": 1, "done": true, "records": [...]}
          }
        }
      ]
    }

- ``request.path`` / ``request.query`` hold the URL path and the decoded query pairs; the host is
  never stored (replay ignores it).
- A body is stored under exactly one of ``json`` (parsed JSON), ``form`` (decoded
  ``application/x-www-form-urlencoded`` pairs), ``text`` (other UTF-8) or ``base64`` (binary);
  a missing key means an empty body. Gzip request bodies are stored decompressed.
- Only a small allow-list of headers is kept (content type, ``Sforce-*``, ``Location``,
  ``Retry-After``, ``Last-Modified``...). ``Authorization`` and cookies are never written.

Redaction, applied before anything touches the disk:

- OAuth secrets anywhere in JSON or form bodies (``access_token``, ``refresh_token``,
  ``id_token``, ``client_secret``, ``client_assertion``, ``assertion``, ``signature``, ``code``,
  ``code_verifier``, ``password``, ``sessionId``...) become ``"<redacted>"``.
- Session ids (``00D…!…``) and ``Bearer …`` values inside any string become ``<redacted>``.
- E-mail addresses become ``user<N>@example.com`` and phone/fax fields ``555-01NN``,
  consistently within one fixture.
- Instance hosts (``*.salesforce.com``, ``*.force.com``) become ``example.my.salesforce.com``.
- With ``mask_ids=True``, Salesforce ids are replaced with fake ids keeping the key prefix
  (consistently, in paths, queries and bodies, so recorded follow-up requests still match).

Replay serves, for each request, the first unused interaction with the same method and path
(and, with ``match_query=True``, the same query string). Unmatched requests raise
:class:`ReplayMismatch`.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode

import httpx

from ._ids import make_id

__all__ = ["FIXTURE_VERSION", "RecordingTransport", "Redactor", "ReplayMismatch", "record", "replay"]

FIXTURE_VERSION = 1
REDACTED = "<redacted>"

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "client_assertion",
        "assertion",
        "signature",
        "password",
        "sessionid",
        "session_id",
        "session_token",
        "token",
        "code",
        "code_verifier",
    }
)
_KEEP_REQUEST_HEADERS = ("content-type", "accept", "if-modified-since", "if-unmodified-since", "if-match")
_KEEP_RESPONSE_HEADERS = (
    "content-type",
    "location",
    "retry-after",
    "last-modified",
    "etag",
    "warning",
)
_SESSION = re.compile(r"00D[A-Za-z0-9]{12,15}![A-Za-z0-9._\-]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=!\-]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_HOST = re.compile(
    r"\b[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.(?:my\.salesforce\.com|salesforce\.com|force\.com|"
    r"salesforce-setup\.com|visualforce\.com|site\.com)\b"
)
_SAFE_HOSTS = frozenset({"login.salesforce.com", "test.salesforce.com", "example.my.salesforce.com"})
_ID = re.compile(r"(?<![A-Za-z0-9])[a-zA-Z0-9]{5}0[a-zA-Z0-9]{9}(?:[a-zA-Z0-9]{3})?(?![A-Za-z0-9])")
_PHONE_KEY = re.compile(r"(?i)(phone|fax)")


class ReplayMismatch(AssertionError):
    """A replayed client made a request that has no (unused) recorded interaction."""


class Redactor:
    """Scrubs secrets and personal data from recorded traffic (see the module docstring).

    Args:
        mask_ids: Also replace Salesforce record ids (consistently) with fake ones.
        mask_emails: Replace e-mail addresses.
        mask_phones: Replace values of ``*Phone*`` / ``*Fax*`` fields.
        mask_hosts: Replace instance host names.
        extra_keys: More JSON/form keys whose values must be redacted.
    """

    def __init__(
        self,
        *,
        mask_ids: bool = False,
        mask_emails: bool = True,
        mask_phones: bool = True,
        mask_hosts: bool = True,
        extra_keys: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self.mask_ids = mask_ids
        self.mask_emails = mask_emails
        self.mask_phones = mask_phones
        self.mask_hosts = mask_hosts
        self.keys = _SECRET_KEYS | {k.lower() for k in extra_keys}
        self._emails: dict[str, str] = {}
        self._phones: dict[str, str] = {}
        self._ids: dict[str, str] = {}
        self._lock = threading.Lock()

    def _email(self, m: re.Match[str]) -> str:
        value = m.group(0)
        if value.endswith("@example.com"):
            return value
        with self._lock:
            return self._emails.setdefault(value, f"user{len(self._emails) + 1}@example.com")

    def _phone(self, value: str) -> str:
        with self._lock:
            return self._phones.setdefault(value, f"555-01{len(self._phones) % 100:02d}")

    def _id(self, m: re.Match[str]) -> str:
        value = m.group(0)
        if not any(c.isdigit() for c in value):
            return value
        key = value[:15]
        with self._lock:
            fake = self._ids.get(key)
            if fake is None:
                fake = make_id(value[:3], len(self._ids) + 1, pod="00")
                self._ids[key] = fake
        return fake if len(value) == 18 else fake[:15]

    @staticmethod
    def _host(m: re.Match[str]) -> str:
        host = m.group(0)
        return host if host.lower() in _SAFE_HOSTS else "example.my.salesforce.com"

    def text(self, value: str) -> str:
        """Redact one string."""
        value = _SESSION.sub(REDACTED, value)
        value = _BEARER.sub(f"Bearer {REDACTED}", value)
        if self.mask_emails:
            value = _EMAIL.sub(self._email, value)
        if self.mask_hosts:
            value = _HOST.sub(self._host, value)
        if self.mask_ids:
            value = _ID.sub(self._id, value)
        return value

    def value(self, obj: Any, key: str | None = None) -> Any:
        """Redact a decoded JSON value recursively."""
        if key is not None and key.lower() in self.keys and obj is not None:
            return REDACTED
        if isinstance(obj, str):
            if self.mask_phones and key is not None and _PHONE_KEY.search(key) and obj:
                return self._phone(obj)
            return self.text(obj)
        if isinstance(obj, Mapping):
            m = cast("Mapping[str, Any]", obj)
            return {str(k): self.value(v, str(k)) for k, v in m.items()}
        if isinstance(obj, list):
            items = cast("list[Any]", obj)
            return [self.value(v, key) for v in items]
        return obj

    def pairs(self, pairs: list[tuple[str, str]]) -> list[list[str]]:
        """Redact decoded query/form pairs."""
        return [[k, REDACTED if k.lower() in self.keys else self.text(v)] for k, v in pairs]


def _body(content: bytes, content_type: str, redactor: Redactor) -> dict[str, Any]:
    if not content:
        return {}
    ctype = content_type.split(";", 1)[0].strip().lower()
    if ctype == "application/json" or ctype.endswith("+json"):
        try:
            return {"json": redactor.value(json.loads(content))}
        except ValueError:
            pass
    if ctype == "application/x-www-form-urlencoded":
        try:
            pairs = parse_qsl(content.decode("utf-8"), keep_blank_values=True)
            return {"form": redactor.pairs(pairs)}
        except UnicodeDecodeError:
            pass
    try:
        return {"text": redactor.text(content.decode("utf-8"))}
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(content).decode("ascii")}


def _headers(headers: httpx.Headers, keep: tuple[str, ...], redactor: Redactor) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers.items():
        low = name.lower()
        if low in ("authorization", "cookie", "set-cookie", "proxy-authorization"):
            continue
        if low in keep or low.startswith("sforce-"):
            out[low] = redactor.text(value)
    return out


def _request_entry(request: httpx.Request, redactor: Redactor) -> dict[str, Any]:
    content = request.content
    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            content = gzip.decompress(content)
        except OSError:
            pass
    entry: dict[str, Any] = {
        "method": request.method,
        "path": redactor.text(request.url.path),
        "query": redactor.pairs(list(request.url.params.multi_items())),
        "headers": _headers(request.headers, _KEEP_REQUEST_HEADERS, redactor),
    }
    entry.update(_body(content, request.headers.get("content-type", ""), redactor))
    return entry


def _response_entry(response: httpx.Response, redactor: Redactor) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "status": response.status_code,
        "headers": _headers(response.headers, _KEEP_RESPONSE_HEADERS, redactor),
    }
    entry.update(_body(response.content, response.headers.get("content-type", ""), redactor))
    return entry


def _plain_response(response: httpx.Response, request: httpx.Request) -> httpx.Response:
    """Re-wrap an already-read response so its (decoded) body can be read again."""
    headers = [
        (k, v)
        for k, v in response.headers.multi_items()
        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
    ]
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=response.content,
        request=request,
        extensions={k: v for k, v in response.extensions.items() if k != "network_stream"},
    )


class RecordingTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """Forwards requests to a real transport and appends redacted exchanges to a fixture file.

    Works with both ``httpx.Client`` and ``httpx.AsyncClient``. The file is rewritten after every
    exchange, so a crashed test run still leaves a usable fixture.

    Args:
        path: Fixture file to write (created or overwritten).
        transport: The transport to forward to. Defaults to ``httpx.HTTPTransport()`` (sync) or
            ``httpx.AsyncHTTPTransport()`` (async), created on first use.
        redactor: Redaction settings.
    """

    def __init__(
        self,
        path: str | Path,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self.path = Path(path)
        self.redactor = redactor or Redactor()
        self._inner = transport
        self._sync: httpx.BaseTransport | None = None
        self._async: httpx.AsyncBaseTransport | None = None
        if isinstance(transport, httpx.BaseTransport):
            self._sync = transport
        if isinstance(transport, httpx.AsyncBaseTransport):
            self._async = transport
        self.interactions: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"esf_fixture": FIXTURE_VERSION, "interactions": self.interactions}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _record(self, request: httpx.Request, response: httpx.Response) -> None:
        entry = {
            "request": _request_entry(request, self.redactor),
            "response": _response_entry(response, self.redactor),
        }
        with self._lock:
            self.interactions.append(entry)
            self._write()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Forward (sync), record, and return the response."""
        if self._sync is None:
            if self._inner is not None:
                raise TypeError("the wrapped transport is async-only; use it with httpx.AsyncClient")
            self._sync = httpx.HTTPTransport()
        request.read()
        response = self._sync.handle_request(request)
        try:
            response.read()
        finally:
            response.close()
        self._record(request, response)
        return _plain_response(response, request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Forward (async), record, and return the response."""
        if self._async is None:
            if self._inner is not None:
                raise TypeError("the wrapped transport is sync-only; use it with httpx.Client")
            self._async = httpx.AsyncHTTPTransport()
        await request.aread()
        response = await self._async.handle_async_request(request)
        try:
            await response.aread()
        finally:
            await response.aclose()
        self._record(request, response)
        return _plain_response(response, request)

    def close(self) -> None:
        """Close the wrapped sync transport."""
        if self._sync is not None:
            self._sync.close()

    async def aclose(self) -> None:
        """Close the wrapped async transport."""
        if self._async is not None:
            await self._async.aclose()


def record(
    path: str | Path,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    *,
    mask_ids: bool = False,
    mask_emails: bool = True,
    mask_phones: bool = True,
    mask_hosts: bool = True,
    extra_keys: frozenset[str] | set[str] = frozenset(),
) -> RecordingTransport:
    """An httpx transport that records redacted exchanges to ``path`` (sync and async).

    Args:
        path: Fixture file to write.
        transport: Transport to forward to (default: a real HTTP transport).
        mask_ids: Replace Salesforce ids with consistent fake ids.
        mask_emails: Replace e-mail addresses.
        mask_phones: Replace phone/fax field values.
        mask_hosts: Replace instance host names with ``example.my.salesforce.com``.
        extra_keys: Additional JSON/form keys to redact.

    Returns:
        The transport; pass it to ``httpx.Client(transport=...)`` or ``httpx.AsyncClient``.
    """
    return RecordingTransport(
        path,
        transport,
        redactor=Redactor(
            mask_ids=mask_ids,
            mask_emails=mask_emails,
            mask_phones=mask_phones,
            mask_hosts=mask_hosts,
            extra_keys=extra_keys,
        ),
    )


def load_fixture(path: str | Path) -> list[dict[str, Any]]:
    """Read and validate a fixture file; returns its interactions."""
    doc: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or cast("dict[str, Any]", doc).get("esf_fixture") != FIXTURE_VERSION:
        raise ValueError(f"{path}: not an esf fixture (expected esf_fixture={FIXTURE_VERSION})")
    items: Any = cast("dict[str, Any]", doc).get("interactions")
    if not isinstance(items, list):
        raise ValueError(f"{path}: 'interactions' must be a list")
    return cast("list[dict[str, Any]]", items)


def _response_from(entry: Mapping[str, Any], request: httpx.Request) -> httpx.Response:
    headers: dict[str, str] = dict(entry.get("headers") or {})
    content = b""
    if "json" in entry:
        content = json.dumps(entry["json"]).encode()
        headers.setdefault("content-type", "application/json")
    elif "form" in entry:
        content = urlencode([tuple(p) for p in entry["form"]]).encode()
    elif "text" in entry:
        content = str(entry["text"]).encode()
    elif "base64" in entry:
        content = base64.b64decode(str(entry["base64"]))
    return httpx.Response(int(entry["status"]), headers=headers, content=content, request=request)


class _Replayer:
    def __init__(self, interactions: list[dict[str, Any]], match_query: bool, repeat: bool) -> None:
        self.interactions = interactions
        self.used = [False] * len(interactions)
        self.match_query = match_query
        self.repeat = repeat
        self.lock = threading.Lock()

    def _matches(self, entry: Mapping[str, Any], request: httpx.Request) -> bool:
        req: Mapping[str, Any] = entry["request"]
        if str(req["method"]).upper() != request.method or req["path"] != request.url.path:
            return False
        if self.match_query:
            pairs = cast("list[list[str]]", req.get("query") or [])
            want = sorted((str(p[0]), str(p[1])) for p in pairs)
            return want == sorted(request.url.params.multi_items())
        return True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            last: int | None = None
            for i, entry in enumerate(self.interactions):
                if not self._matches(entry, request):
                    continue
                last = i
                if not self.used[i]:
                    self.used[i] = True
                    return _response_from(entry["response"], request)
            if self.repeat and last is not None:
                return _response_from(self.interactions[last]["response"], request)
        raise ReplayMismatch(
            f"no unused recorded interaction for {request.method} {request.url.path}"
            f" ({sum(self.used)}/{len(self.used)} used)"
        )

    @property
    def remaining(self) -> int:
        return self.used.count(False)


def replay(
    path: str | Path,
    *,
    match_query: bool = False,
    repeat: bool = False,
) -> httpx.MockTransport:
    """An ``httpx.MockTransport`` serving the interactions recorded in ``path``.

    Args:
        path: Fixture written by :func:`record` (or by hand; see the module docstring).
        match_query: Also require the query string to match.
        repeat: When all matching interactions were used, serve the last one again instead of
            raising :class:`ReplayMismatch`.

    Returns:
        A transport usable with both ``httpx.Client`` and ``httpx.AsyncClient``. Its
        ``handler.remaining`` attribute counts interactions not served yet.
    """
    handler: Callable[[httpx.Request], httpx.Response] = _Replayer(load_fixture(path), match_query, repeat)
    return httpx.MockTransport(handler)
