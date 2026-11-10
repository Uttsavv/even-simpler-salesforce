"""The ``esf`` command-line tool (stdlib only + the library's sync client).

Usage::

    esf whoami [--json]
    esf limits [--json]
    esf query "SELECT Id, Name FROM Lead LIMIT 10" [--json | --csv] [--all]
    esf describe Lead [--json]
    esf request GET sobjects/Lead/describe [--json BODY | --json @FILE] [--param KEY=VALUE]
    esf generate models Lead Account [--out models.py] [--pydantic]
    esf version

Login comes from the environment only (first complete set wins):

- ``ESF_ACCESS_TOKEN`` + ``ESF_INSTANCE_URL`` (an existing session),
- ``ESF_DOMAIN`` + ``ESF_CLIENT_ID`` + ``ESF_USERNAME`` + ``ESF_PRIVATE_KEY_FILE`` (JWT bearer),
- ``ESF_DOMAIN`` + ``ESF_CLIENT_ID`` + ``ESF_CLIENT_SECRET`` (client credentials).

``ESF_API_VERSION`` optionally pins the API version. Tokens are never printed.

Exit codes: ``0`` success, ``1`` Salesforce/API error, ``2`` usage or configuration error,
``130`` interrupted.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from .._version import __version__
from ..core.records import flatten
from ._env import EnvConfigError, auth_from_env, client_from_env
from ._generate import add_generate_parser

if TYPE_CHECKING:
    from ..errors import SalesforceError

__all__ = ["EXIT_API_ERROR", "EXIT_INTERRUPTED", "EXIT_OK", "EXIT_USAGE", "main"]

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting, so ``main`` always returns a code."""

    def error(self, message: str) -> Any:  # pragma: no cover - exercised via main
        self.print_usage(sys.stderr)
        raise _UsageError(message)


class _UsageError(Exception):
    pass


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="esf",
        description="even-simpler-salesforce command-line tool. Login is read from ESF_* variables.",
    )
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    w = sub.add_parser("whoami", help="show the authenticated user and org")
    w.add_argument("--json", action="store_true", help="print JSON")

    lim = sub.add_parser("limits", help="show org limits (costs one API call)")
    lim.add_argument("--json", action="store_true", help="print JSON")

    q = sub.add_parser("query", help="run a SOQL query")
    q.add_argument("soql", help="the SOQL query")
    fmt = q.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print a JSON array")
    fmt.add_argument("--csv", action="store_true", help="print CSV")
    q.add_argument("--all", action="store_true", help="fetch every page (default: first page only)")
    q.add_argument("--include-deleted", action="store_true", help="use queryAll")
    q.add_argument("--page-size", type=int, default=None, help="batch size hint (200-2000)")

    sub.add_parser("version", help="print versions")

    sub.add_parser(
        "migrate", help="rewrite simple-salesforce code (needs the [migrate] extra)", add_help=False
    )

    d = sub.add_parser("describe", help="show an sObject's fields (costs one API call)")
    d.add_argument("sobject", help="sObject API name, e.g. Lead")
    d.add_argument("--json", action="store_true", help="print JSON")

    r = sub.add_parser("request", help="call any REST resource with sf.request")
    r.add_argument("method", type=str.upper, help="HTTP method, e.g. GET")
    r.add_argument("path", help="path under /services/data/vXX.X/, or absolute under /services/")
    r.add_argument("--json", dest="body", metavar="JSON", help="JSON request body; @FILE reads a file")
    r.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE", help="query parameter (repeatable)"
    )

    add_generate_parser(sub)
    return p


# --- output helpers -------------------------------------------------------------------


def _strip(value: Any) -> Any:
    """Drop ``attributes`` at every level (JSON output keeps the nested shape)."""
    if isinstance(value, Mapping):
        m: Mapping[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        return {k: _strip(v) for k, v in m.items() if k != "attributes"}
    if isinstance(value, list):
        items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
        return [_strip(v) for v in items]
    return value


def _columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    cols: dict[str, None] = {}
    for r in rows:
        for k in r:
            cols[k] = None
    return list(cols)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# ASCII markers, plus the full-width forms some spreadsheet locales also treat as formulas.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20")


def _printable(text: str) -> str:
    """Escape control characters (ANSI escapes, newlines) so org data can't drive the terminal."""
    return _CONTROL.sub(lambda m: repr(m.group(0))[1:-1], text)


def _csv_cell(value: Any) -> str:
    """A CSV cell that spreadsheets won't run as a formula (OWASP CSV injection guidance)."""
    text = _cell(value)
    if isinstance(value, str) and text.startswith(_FORMULA_START):
        return "'" + text
    return text


def _table(rows: Sequence[Mapping[str, Any]], cols: Sequence[str], out: TextIO) -> None:
    cells = [[_printable(_cell(r.get(c))) for c in cols] for r in rows]
    cols = [_printable(c) for c in cols]
    widths = [max([len(c), *(len(row[i]) for row in cells)]) for i, c in enumerate(cols)]
    widths = [min(w, 60) for w in widths]

    def line(values: Iterable[str]) -> str:
        parts = [
            (v if len(v) <= w else v[: w - 1] + "…").ljust(w) for v, w in zip(values, widths, strict=False)
        ]
        return "  ".join(parts).rstrip()

    out.write(line(cols) + "\n")
    out.write(line("-" * w for w in widths) + "\n")
    for row in cells:
        out.write(line(row) + "\n")


def _json(value: Any, out: TextIO) -> None:
    out.write(json.dumps(value, indent=2, default=str, ensure_ascii=False) + "\n")


# --- commands ---------------------------------------------------------------------------


def _client(env: Mapping[str, str]) -> Any:
    return client_from_env(env)


def _cmd_whoami(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    with _client(env) as sf:
        info: dict[str, Any] = sf.auth.userinfo()
        token = sf.auth.token()
        result = {
            "name": info.get("name"),
            "username": info.get("preferred_username"),
            "email": info.get("email"),
            "user_id": info.get("user_id") or token.user_id,
            "org_id": info.get("organization_id") or token.org_id,
            "instance_url": token.instance_url,
            "api_version": sf.api_version,
            "auth": auth_from_env(env, read_key=False).kind,
        }
    if args.json:
        _json(result, out)
    else:
        width = max(len(k) for k in result)
        for k, v in result.items():
            out.write(f"{k.replace('_', ' ').ljust(width)}  {_printable(_cell(v))}\n")
    return EXIT_OK


def _cmd_limits(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    with _client(env) as sf:
        limits = sf.limits()
    if args.json:
        _json({k: {"max": v.max, "remaining": v.remaining, "used": v.used} for k, v in limits.items()}, out)
        return EXIT_OK
    rows = [
        {
            "limit": name,
            "used": lim.used,
            "max": lim.max,
            "remaining": lim.remaining,
            "used %": f"{lim.used / lim.max:.1%}" if lim.max else "-",
        }
        for name, lim in sorted(limits.items())
    ]
    _table(rows, ["limit", "used", "max", "remaining", "used %"], out)
    return EXIT_OK


def _json_stream(rows: Iterable[Any], out: TextIO) -> int:
    """Write ``rows`` as one JSON array, row by row; the text equals :func:`_json` of the list."""
    count = 0
    for row in rows:
        item = json.dumps(_strip(row), indent=2, default=str, ensure_ascii=False)
        # JSON strings never hold a raw newline, so indenting every line is safe.
        out.write(("[\n" if count == 0 else ",\n") + "\n".join("  " + line for line in item.split("\n")))
        count += 1
    out.write("\n]\n" if count else "[]\n")
    return count


def _cmd_query(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    opts: dict[str, Any] = {"page_size": args.page_size, "include_deleted": args.include_deleted}
    total: int | None = None
    more = False
    with _client(env) as sf:
        if args.all:
            records: Iterable[Mapping[str, Any]] = sf.query_iter(args.soql, **opts)
        else:
            page = sf.query(args.soql, **opts)
            records, total, more = page.records, page.total_size, not page.done
        if args.json:
            # Printed while later pages are fetched, so a large export starts at once.
            count = _json_stream(records, out)
        else:
            # CSV and table columns depend on every row, so they wait for the last page.
            # Only the flattened copy of each row is kept.
            rows = [flatten(r) for r in records]
            count = len(rows)
            if args.csv:
                writer = csv.DictWriter(
                    out, fieldnames=_columns(rows), extrasaction="ignore", lineterminator="\n"
                )
                writer.writeheader()
                for r in rows:
                    writer.writerow({k: _csv_cell(v) for k, v in r.items()})
            elif rows:
                _table(rows, _columns(rows), out)
    if total is None:
        total = count
    noun = "row" if total == 1 else "rows"
    if more:
        err.write(f"{count} of {total} {noun} (more available; use --all)\n")
    else:
        err.write(f"{count} {noun}\n")
    return EXIT_OK


def _describe_rows(describe: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per field: name, type, length, required, picklist values.

    ``required`` means Salesforce needs a value on create: not nillable, createable and not
    defaulted on create (``Field`` properties of *DescribeSObjectResult*).
    """
    fields: Any = describe.get("fields")
    rows: list[dict[str, Any]] = []
    for f in fields if isinstance(fields, list) else []:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(f, Mapping):
            continue
        field: Mapping[str, Any] = f  # pyright: ignore[reportUnknownVariableType]
        values: Any = field.get("picklistValues")
        active = [
            str(v.get("value"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            for v in (values if isinstance(values, list) else [])  # pyright: ignore[reportUnknownVariableType]
            if isinstance(v, Mapping) and v.get("active", True)  # pyright: ignore[reportUnknownMemberType]
        ]
        rows.append(
            {
                "name": field.get("name"),
                "type": field.get("type"),
                "length": field.get("length") or None,
                "required": (
                    field.get("nillable") is False
                    and field.get("createable") is True
                    and not field.get("defaultedOnCreate")
                ),
                "restricted": bool(field.get("restrictedPicklist")),
                "values": active,
            }
        )
    return rows


def _cmd_describe(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    with _client(env) as sf:
        describe = sf.sobject(args.sobject).describe()
    rows = _describe_rows(describe)
    if args.json:
        _json(rows, out)
        return EXIT_OK
    table = [
        {
            "field": r["name"],
            "type": r["type"],
            "length": r["length"],
            "required": "yes" if r["required"] else "",
            "values": ("(restricted) " if r["restricted"] else "") + ", ".join(r["values"])
            if r["values"]
            else "",
        }
        for r in rows
    ]
    _table(table, ["field", "type", "length", "required", "values"], out)
    return EXIT_OK


def _request_body(text: str | None) -> Any:
    if text is None:
        return None
    if text.startswith("@"):
        try:
            text = Path(text[1:]).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read {text[1:]}: {exc.strerror}") from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"--json is not valid JSON: {exc}") from None


def _cmd_request(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    params: dict[str, str] = {}
    for item in args.param:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--param needs KEY=VALUE, got {item!r}")
        params[key] = value
    body = _request_body(args.body)
    with _client(env) as sf:
        result = sf.request(args.method, args.path, params=params or None, json=body)
    if result is None:
        return EXIT_OK
    if isinstance(result, bytes):
        buffer = getattr(out, "buffer", None)
        if buffer is None:  # pragma: no cover - a text-only stdout
            raise ValueError("the response is binary and stdout can't take bytes")
        out.flush()
        buffer.write(result)
        return EXIT_OK
    if isinstance(result, str):
        # Org text can hold escape sequences; a terminal gets them escaped, a pipe gets them raw.
        out.write(_printable(result) + "\n" if out.isatty() else result)
        return EXIT_OK
    _json(result, out)
    return EXIT_OK


def _cmd_version(out: TextIO) -> int:
    import platform

    import httpx

    from .._async.client import DEFAULT_API_VERSION

    out.write(f"esf {__version__} (even-simpler-salesforce)\n")
    out.write(f"api version {DEFAULT_API_VERSION}, httpx {httpx.__version__}, ")
    out.write(f"python {platform.python_version()}\n")
    return EXIT_OK


def _write_error(exc: SalesforceError, err: TextIO) -> None:
    """One escaped error line, then the hint and the docs link on their own lines."""
    hint, docs_url = exc.hint, exc.docs_url
    plain = copy.copy(exc)
    plain.hint = plain.docs_url = None  # str() without them; they are printed below
    # The message comes from Salesforce and can hold terminal escape sequences.
    err.write(f"esf: {type(exc).__name__}: {_printable(str(plain))}\n")
    if hint:
        err.write(f"  hint: {_printable(hint)}\n")
    if docs_url:
        err.write(f"  docs: {docs_url}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``esf`` CLI and return its exit code.

    Args:
        argv: Arguments without the program name (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``1`` on a Salesforce/API error, ``2`` on a usage or configuration
        error, ``130`` when interrupted.
    """
    out, err = sys.stdout, sys.stderr
    from ..errors import SalesforceError

    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw[:1] == ["migrate"]:  # its own parser (§9.19); no Salesforce login involved
        from .._migrate import main as migrate_main

        return migrate_main(raw[1:], out=out, err=err)
    try:
        args = _build_parser().parse_args(list(argv) if argv is not None else None)
    except _UsageError as exc:
        err.write(f"esf: error: {exc}\n")
        return EXIT_USAGE
    except SystemExit as exc:  # --help / --version style exits from argparse
        return int(exc.code or 0) if isinstance(exc.code, int) else EXIT_OK
    env = os.environ
    try:
        if args.command == "version":
            return _cmd_version(out)
        if args.command == "generate":
            return int(args.handler(args, env, out, err))
        if args.command == "describe":
            return _cmd_describe(args, env, out)
        if args.command == "request":
            return _cmd_request(args, env, out)
        if args.command == "whoami":
            return _cmd_whoami(args, env, out)
        if args.command == "limits":
            return _cmd_limits(args, env, out)
        if args.command == "query":
            return _cmd_query(args, env, out, err)
    except EnvConfigError as exc:
        err.write(f"esf: configuration error: {exc}\n")
        return EXIT_USAGE
    except SalesforceError as exc:
        _write_error(exc, err)
        return EXIT_API_ERROR
    except ValueError as exc:
        err.write(f"esf: error: {_printable(str(exc))}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        err.write("esf: interrupted\n")
        return EXIT_INTERRUPTED
    except BrokenPipeError:  # pragma: no cover - e.g. `esf query ... | head`
        return EXIT_OK
    err.write(f"esf: unknown command {args.command!r}\n")  # pragma: no cover
    return EXIT_USAGE  # pragma: no cover
