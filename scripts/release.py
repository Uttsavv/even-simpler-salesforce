#!/usr/bin/env python3
"""Cut a release: bump the version, compile the changelog, commit and tag.

Usage::

    uv run scripts/release.py 0.2.0
    uv run scripts/release.py 0.2.0rc1          # pre-release: changelog fragments are kept
    uv run scripts/release.py 0.2.0 --dry-run   # show what would happen, change nothing

Steps (final releases):

1. validate the version (PEP 440 public version, greater than the current one);
2. check the working tree is clean and the tag does not exist yet;
3. write ``__version__`` in ``src/even_simpler_salesforce/_version.py``;
4. ``towncrier build --yes --version X`` (consumes ``changelog.d/`` into ``CHANGELOG.md``);
5. ``git commit -s`` and ``git tag -a vX``.

Nothing is pushed. The script prints the push command; pushing the tag triggers
``.github/workflows/release.yml``. Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "src" / "even_simpler_salesforce" / "_version.py"
VERSION_LINE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\'][ \t]*$', re.MULTILINE)

# PEP 440 public version (no epoch, no local segment), canonical spelling only.
PEP440 = re.compile(
    r"""
    ^(?P<release>\d+(?:\.\d+)*)
    (?:(?P<pre_l>a|b|rc)(?P<pre_n>\d+))?
    (?:\.post(?P<post>\d+))?
    (?:\.dev(?P<dev>\d+))?$
    """,
    re.VERBOSE,
)
_PRE_ORDER = {"a": 0, "b": 1, "rc": 2}


class ReleaseError(Exception):
    pass


def parse_version(v: str) -> re.Match[str]:
    m = PEP440.match(v)
    if not m:
        raise ReleaseError(
            f"{v!r} is not a canonical PEP 440 version (examples: 0.2.0, 0.2.0rc1, 1.0.0.post1)"
        )
    if len(m["release"].split(".")) != 3:
        raise ReleaseError(f"{v!r}: use MAJOR.MINOR.PATCH (SemVer), e.g. 0.2.0")
    return m


def sort_key(v: str) -> tuple[object, ...]:
    """Ordering per PEP 440 for the subset accepted by ``parse_version``-like strings."""
    m = PEP440.match(v)
    if not m:
        raise ReleaseError(f"cannot compare unparseable version {v!r}")
    release = tuple(int(x) for x in m["release"].split("."))
    release = release + (0,) * (3 - len(release))
    dev = int(m["dev"]) if m["dev"] is not None else None
    post = int(m["post"]) if m["post"] is not None else None
    if m["pre_l"]:
        pre: tuple[float, int] = (_PRE_ORDER[m["pre_l"]], int(m["pre_n"]))
    elif dev is not None and post is None:
        pre = (-1, 0)  # X.Y.Z.devN sorts before X.Y.Za0
    else:
        pre = (float("inf"), 0)
    return (
        release,
        pre,
        post if post is not None else -1,
        dev if dev is not None else float("inf"),
    )


def is_prerelease(v: str) -> bool:
    m = parse_version(v)
    return bool(m["pre_l"] or m["dev"])


def current_version() -> str:
    m = VERSION_LINE.search(VERSION_FILE.read_text(encoding="utf-8"))
    if not m:
        raise ReleaseError(f"no __version__ line found in {VERSION_FILE}")
    return m.group(1)


def git(*args: str, capture: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=ROOT, check=True, text=True, capture_output=capture)
    return res.stdout.strip() if capture else ""


def towncrier_cmd() -> list[str]:
    """towncrier from the active env (``uv run`` installs the dev group), else via uvx."""
    candidates: list[list[str]] = []
    if exe := shutil.which("towncrier"):
        candidates.append([exe])
    candidates.append([sys.executable, "-m", "towncrier"])
    if uvx := shutil.which("uvx"):
        candidates.append([uvx, "towncrier"])
    for cmd in candidates:
        probe = subprocess.run([*cmd, "--version"], cwd=ROOT, capture_output=True, text=True)
        if probe.returncode == 0:
            return cmd
    raise ReleaseError("towncrier not found; run via `uv run scripts/release.py ...` or install it")


def run(cmd: list[str], *, dry_run: bool) -> None:
    print("+", " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, cwd=ROOT, check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("version", help="new version, e.g. 0.2.0 or 0.2.0rc1 (no leading 'v')")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument(
        "--allow-dirty", action="store_true", help="skip the clean-working-tree check (not recommended)"
    )
    ap.add_argument(
        "--compile-changelog",
        action="store_true",
        help="also consume changelog fragments for a pre-release (default: only for final releases)",
    )
    ns = ap.parse_args(argv)

    version: str = ns.version.removeprefix("v")
    dry: bool = ns.dry_run
    try:
        parse_version(version)
        old = current_version()
        if sort_key(version) <= sort_key(old):
            raise ReleaseError(f"new version {version} must be greater than current {old}")
        tag = f"v{version}"
        if git("tag", "--list", tag):
            raise ReleaseError(f"tag {tag} already exists")
        if not ns.allow_dirty and git("status", "--porcelain"):
            raise ReleaseError("working tree is not clean; commit or stash first (or --allow-dirty)")
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            print(f"warning: releasing from branch {branch!r}, not 'main'", file=sys.stderr)
        pre = is_prerelease(version)
        compile_changelog = not pre or ns.compile_changelog
        tc = towncrier_cmd() if compile_changelog else []
    except (ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{'[dry-run] ' if dry else ''}release {old} -> {version}{' (pre-release)' if pre else ''}")

    # 1. bump _version.py
    rel = VERSION_FILE.relative_to(ROOT)
    print(f"+ set __version__ = {version!r} in {rel}")
    if not dry:
        original = VERSION_FILE.read_text(encoding="utf-8")
        text = VERSION_LINE.sub(f'__version__ = "{version}"', original, count=1)
        VERSION_FILE.write_text(text, encoding="utf-8")

    # 2. changelog
    if compile_changelog:
        if dry:
            run([*tc, "build", "--draft", "--version", version], dry_run=False)
        run([*tc, "build", "--yes", "--version", version], dry_run=dry)
    else:
        print("pre-release: keeping changelog.d/ fragments (the GitHub Release uses a draft)")

    # 3. commit + tag
    run(["git", "add", "-A", "--", str(rel), "CHANGELOG.md", "changelog.d"], dry_run=dry)
    run(["git", "commit", "-s", "-m", f"release: {version}"], dry_run=dry)
    run(["git", "tag", "-a", tag, "-m", f"Release {version}"], dry_run=dry)

    print()
    print("Next: review the commit, then push it and the tag to trigger the release workflow:")
    print(f"    git push origin HEAD {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
