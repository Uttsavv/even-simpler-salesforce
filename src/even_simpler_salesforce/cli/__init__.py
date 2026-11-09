"""The ``esf`` command-line tool (stdlib only + the library's sync client).

Usage::

    esf whoami [--json]
    esf limits [--json]
    esf query "SELECT Id, Name FROM Lead LIMIT 10" [--json | --csv] [--all]
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
import csv
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, TextIO

from .._version import __version__
from ._env import EnvConfigError, auth_from_env

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

    g = sub.add_parser("generate", help="code generation (available from 0.2)")
    g.add_argument("what", nargs="?", default="actions")
    g.add_argument("rest", nargs=argparse.REMAINDER)
    return p


# --- output helpers -------------------------------------------------------------------


def _flatten(row: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key == "attributes":
            continue
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            nested: Mapping[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
            if "records" in nested:
                out[name] = json.dumps(_strip(nested.get("records") or []), default=str)
            else:
                out.update(_flatten(nested, name + "."))
        else:
            out[name] = value
    return out


def _strip(value: Any) -> Any:
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


def _table(rows: Sequence[Mapping[str, Any]], cols: Sequence[str], out: TextIO) -> None:
    cells = [[_cell(r.get(c)) for c in cols] for r in rows]
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
    from .. import Salesforce

    auth = auth_from_env(env)
    kwargs: dict[str, Any] = {"user_agent": f"esf-cli/{__version__}"}
    version = env.get("ESF_API_VERSION", "").strip()
    if version:
        kwargs["api_version"] = version.removeprefix("v")
    try:
        return Salesforce(auth, **kwargs)
    except ValueError as exc:
        raise EnvConfigError(str(exc)) from None


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
            out.write(f"{k.replace('_', ' ').ljust(width)}  {_cell(v)}\n")
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


def _cmd_query(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    with _client(env) as sf:
        if args.all:
            records = list(
                sf.query_iter(args.soql, page_size=args.page_size, include_deleted=args.include_deleted)
            )
            total, more = len(records), False
        else:
            page = sf.query(args.soql, page_size=args.page_size, include_deleted=args.include_deleted)
            records, total, more = list(page.records), page.total_size, not page.done
    if args.json:
        _json(_strip(records), out)
    elif args.csv:
        rows = [_flatten(r) for r in records]
        writer = csv.DictWriter(out, fieldnames=_columns(rows), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _cell(v) for k, v in r.items()})
    else:
        rows = [_flatten(r) for r in records]
        if rows:
            _table(rows, _columns(rows), out)
    noun = "row" if total == 1 else "rows"
    if more:
        err.write(f"{len(records)} of {total} {noun} (more available; use --all)\n")
    else:
        err.write(f"{len(records)} {noun}\n")
    return EXIT_OK


def _cmd_version(out: TextIO) -> int:
    import platform

    import httpx

    from .._async.client import DEFAULT_API_VERSION

    out.write(f"esf {__version__} (even-simpler-salesforce)\n")
    out.write(f"api version {DEFAULT_API_VERSION}, httpx {httpx.__version__}, ")
    out.write(f"python {platform.python_version()}\n")
    return EXIT_OK


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
            err.write(f"esf: 'esf generate {args.what}' is not available until 0.2\n")
            return EXIT_USAGE
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
        err.write(f"esf: {type(exc).__name__}: {exc}\n")
        return EXIT_API_ERROR
    except ValueError as exc:
        err.write(f"esf: error: {exc}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        err.write("esf: interrupted\n")
        return EXIT_INTERRUPTED
    except BrokenPipeError:  # pragma: no cover - e.g. `esf query ... | head`
        return EXIT_OK
    err.write(f"esf: unknown command {args.command!r}\n")  # pragma: no cover
    return EXIT_USAGE  # pragma: no cover
