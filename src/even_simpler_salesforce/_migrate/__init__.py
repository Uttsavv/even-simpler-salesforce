"""``esf migrate``: rewrite simple-salesforce code to even-simpler-salesforce (§9.19).

Needs the ``[migrate]`` extra (libcst). Run ``esf migrate --help``. The rule ids are documented
at https://uttsavv.github.io/even-simpler-salesforce/latest/migrate/ .
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, TextIO

from .._version import __version__
from ._findings import MARKER_RE, RULES, FileResult, Finding

__all__ = ["main", "migrate_files"]

ALWAYS_SKIP = (".venv", "venv", "site-packages", "node_modules", ".git", "__pycache__", ".tox", ".nox")
EXIT_OK, EXIT_REMAINING, EXIT_ERROR = 0, 1, 2


def _is_test_file(path: Path) -> bool:
    name = path.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or "tests" in path.parts[:-1]
    )


def iter_python_files(paths: Iterable[str], exclude: Sequence[str] = ()) -> Iterator[Path]:
    """Every ``.py`` file under ``paths``, skipping virtualenvs, VCS dirs and ``exclude`` globs."""
    seen: set[Path] = set()
    for raw in paths:
        root = Path(raw)
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for f in files:
            if f.suffix != ".py" or any(part in ALWAYS_SKIP for part in f.parts):
                continue
            rel = f.as_posix()
            if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(f.name, g) for g in exclude):
                continue
            if f not in seen:
                seen.add(f)
                yield f


def migrate_files(
    files: Iterable[Path],
    *,
    client_names: Iterable[str] = (),
    rules: Iterable[str] | None = None,
) -> list[FileResult]:
    """Migrate ``files`` in memory (nothing is written)."""
    from ._engine import Options, transform_source

    names = frozenset(client_names)
    rule_set = frozenset(rules) if rules is not None else None
    out: list[FileResult] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            out.append(FileResult(path.as_posix(), "", "", error=f"read error: {exc}"))
            continue
        opts = Options(client_names=names, rules=rule_set, is_test_file=_is_test_file(path))
        try:
            updated, findings = transform_source(source, opts)
        except Exception as exc:  # libcst.ParserSyntaxError and anything unexpected in one file
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            out.append(FileResult(path.as_posix(), source, source, error=f"parse error: {first}"))
            continue
        for f in findings:
            f.file = path.as_posix()
        out.append(FileResult(path.as_posix(), source, updated, findings))
    return out


def _shift_lines(findings: list[Finding]) -> None:
    """Line numbers after ``--write``: markers inserted above earlier (or the same) statements."""
    inserted = sorted({(f.holder_line, f.markers_added) for f in findings if f.markers_added})
    for f in findings:
        shift = sum(n for line, n in inserted if line <= f.line)
        f.line += shift
        f.end_line += shift


def _remaining_markers(text: str) -> int:
    return sum(1 for mm in MARKER_RE.finditer(text) if mm.group("kind") in ("TODO", "REVIEW"))


def build_report(results: list[FileResult], mode: str) -> dict[str, Any]:
    findings = [f for r in results for f in r.findings]
    return {
        "version": 1,
        "esf_version": __version__,
        "mode": mode,
        "summary": {
            "files": len(results),
            "rewritten": sum(1 for f in findings if f.status == "rewritten"),
            "needs_review": sum(1 for f in findings if f.status == "needs-review"),
            "todo": sum(1 for f in findings if f.status == "todo"),
            "errors": sum(1 for r in results if r.error),
        },
        "findings": [f.as_json() for f in findings],
        "errors": [{"file": r.path, "message": r.error} for r in results if r.error],
    }


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="esf migrate",
        description="Rewrite simple-salesforce code to even-simpler-salesforce. Dry run by default.",
    )
    p.add_argument("paths", nargs="+", help="files or directories")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="apply rewrites and insert markers in place")
    mode.add_argument("--check", action="store_true", help="change nothing; exit 1 while work remains")
    p.add_argument("--report", metavar="FILE", help="write the JSON report to FILE ('-' for stdout)")
    p.add_argument(
        "--client-names", metavar="NAME,...", default="", help="names that hold a Salesforce client"
    )
    p.add_argument("--rules", metavar="RULE,...", default="", help="run only these rule ids")
    p.add_argument("--exclude", metavar="GLOB,...", default="", help="skip matching paths")
    return p


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Run ``esf migrate``; returns 0 (nothing left), 1 (review/TODO left) or 2 (error)."""
    return _run(argv, out or sys.stdout, err or sys.stderr)


def _run(argv: Sequence[str] | None, out: TextIO, err: TextIO) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_ERROR if exc.code not in (0, None) else EXIT_OK
    rules = _split(args.rules) or None
    if rules is not None:
        unknown = sorted(set(rules) - set(RULES))
        if unknown:
            err.write(f"esf migrate: unknown rule(s): {', '.join(unknown)}; known: {', '.join(RULES)}\n")
            return EXIT_ERROR
    try:
        import libcst  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        err.write("esf migrate needs the [migrate] extra: pip install 'even-simpler-salesforce[migrate]'\n")
        return EXIT_ERROR
    missing = [p for p in args.paths if not Path(p).exists()]
    if missing:
        err.write(f"esf migrate: no such file or directory: {', '.join(missing)}\n")
        return EXIT_ERROR

    files = list(iter_python_files(args.paths, _split(args.exclude)))
    results = migrate_files(files, client_names=_split(args.client_names), rules=rules)
    mode = "write" if args.write else "dry-run"

    if args.write:
        for r in results:
            if r.changed and not r.error:
                Path(r.path).write_text(r.updated, encoding="utf-8")
            _shift_lines(r.findings)
    elif not args.check:
        for r in results:
            if r.changed:
                out.writelines(
                    difflib.unified_diff(
                        r.original.splitlines(keepends=True),
                        r.updated.splitlines(keepends=True),
                        fromfile=f"a/{r.path}",
                        tofile=f"b/{r.path}",
                    )
                )

    report = build_report(results, mode)
    if args.report:
        text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if args.report == "-":
            out.write(text)
        else:
            Path(args.report).write_text(text, encoding="utf-8")

    s = report["summary"]
    for e in report["errors"]:
        err.write(f"esf migrate: {e['file']}: {e['message']}\n")
    remaining = s["needs_review"] + s["todo"]
    if args.check:
        # markers left in files + anything this run would still change or mark
        remaining += sum(1 for r in results if r.changed)
        remaining += sum(_remaining_markers(r.original) for r in results)
    label = "check" if args.check else mode
    err.write(
        f"esf migrate ({label}): {s['files']} files, {s['rewritten']} rewritten, "
        f"{s['needs_review']} need review, {s['todo']} TODO, {s['errors']} errors\n"
    )
    if s["errors"]:
        return EXIT_ERROR
    if args.check:
        return EXIT_REMAINING if remaining else EXIT_OK
    return EXIT_REMAINING if (s["needs_review"] or s["todo"]) else EXIT_OK
