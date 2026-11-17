"""``esf skills``: the agent skills that ship in the wheel (``even_simpler_salesforce/skills``).

Each skill is a directory with a ``SKILL.md`` in the open Agent Skills format: YAML front matter
with ``name`` and ``description``, then Markdown. Supporting files sit next to it. The skills
ship with the library, so an installed copy matches the installed library version.

    esf skills list
    esf skills show esf-usage
    esf skills install [NAME ...] [--dir DIR] [--force]

Stdlib only: the files are read with :mod:`importlib.resources`.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    if sys.version_info >= (3, 11):
        from importlib.resources.abc import Traversable
    else:
        from importlib.abc import Traversable

__all__ = ["DEFAULT_DIR", "add_skills_parser", "front_matter", "skills"]

#: Where ``esf skills install`` writes by default: the project directory of Claude Code.
DEFAULT_DIR = ".claude/skills"
#: Files that are build output, never part of a skill.
_IGNORED = frozenset({"__pycache__", ".DS_Store"})


def skills() -> dict[str, Traversable]:
    """The shipped skills by name, sorted: every directory under ``skills/`` with a ``SKILL.md``."""
    root = resources.files("even_simpler_salesforce").joinpath("skills")
    found = {d.name: d for d in root.iterdir() if d.is_dir() and d.joinpath("SKILL.md").is_file()}
    return dict(sorted(found.items()))


def front_matter(text: str) -> dict[str, str]:
    """The ``key: value`` pairs of the YAML front matter of a ``SKILL.md``.

    Reads the subset the shipped skills use: plain one-line values and folded block scalars
    (``key: >-`` followed by indented lines, joined with spaces). Returns ``{}`` without front
    matter.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if key is not None and line.startswith((" ", "\t")):
            out[key] = f"{out[key]} {line.strip()}".strip()
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            key = None
            continue
        key, value = name.strip(), value.strip()
        out[key] = "" if value in (">", ">-", "|", "|-") else value.strip("\"'")
    return out


def _named(names: list[str]) -> dict[str, Traversable]:
    available = skills()
    unknown = [n for n in names if n not in available]
    if unknown:
        raise ValueError(
            f"unknown skill {', '.join(unknown)}; run 'esf skills list' (have: {', '.join(available)})"
        )
    return {n: available[n] for n in names} if names else available


def _cmd_list(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    found = skills()
    width = max((len(n) for n in found), default=0)
    for name, skill in found.items():
        meta = front_matter(skill.joinpath("SKILL.md").read_text(encoding="utf-8"))
        out.write(f"{name.ljust(width)}  {meta.get('description', '')}\n")
    return 0


def _cmd_show(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    skill = _named([args.name])[args.name]
    out.write(skill.joinpath("SKILL.md").read_text(encoding="utf-8"))
    return 0


def _copy(src: Traversable, dest: Path, written: list[Path]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir(), key=lambda t: t.name):
        if item.name in _IGNORED or item.name.endswith(".pyc"):
            continue
        target = dest / item.name
        if item.is_dir():
            _copy(item, target, written)
        else:
            target.write_bytes(item.read_bytes())
            written.append(target)


def _cmd_install(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    chosen = _named(list(dict.fromkeys(args.names)))
    base = Path(args.dir).expanduser()
    existing = [base / n for n in chosen if (base / n).exists() or (base / n).is_symlink()]
    if existing and not args.force:
        raise ValueError(f"{', '.join(str(p) for p in existing)} already exists; pass --force to replace it")
    written: list[Path] = []
    try:
        for name, skill in chosen.items():
            target = base / name
            if args.force:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    shutil.rmtree(target)
            else:
                # Claim the directory: if another install created it since the check above,
                # fail instead of writing into (or deleting) its files.
                base.mkdir(parents=True, exist_ok=True)
                try:
                    target.mkdir()
                except FileExistsError:
                    raise ValueError(f"{target} already exists; pass --force to replace it") from None
            _copy(skill, target, written)
    except OSError as exc:
        raise ValueError(f"cannot write {exc.filename or base}: {exc.strerror}") from None
    for path in written:
        out.write(f"wrote {path}\n")
    err.write(f"installed {len(chosen)} skill{'' if len(chosen) == 1 else 's'} in {base}\n")
    return 0


def add_skills_parser(sub: Any) -> None:
    """Add ``esf skills`` and its actions to the top-level subparsers ``sub``."""
    s = sub.add_parser(
        "skills",
        help="list, show and install the agent skills that ship with the library",
        description=(
            "Agent skills (SKILL.md files) for coding agents such as Claude Code and Codex. "
            "They ship with the library, so they match the installed version: after an "
            "upgrade, run 'esf skills install --force' again, with the same --dir."
        ),
    )
    actions = s.add_subparsers(dest="skills_command", metavar="ACTION")
    actions.required = True

    ls = actions.add_parser("list", help="print the name and description of each skill")
    ls.set_defaults(handler=_cmd_list)

    show = actions.add_parser("show", help="print a skill's SKILL.md")
    show.add_argument("name", help="skill name, from 'esf skills list'")
    show.set_defaults(handler=_cmd_show)

    inst = actions.add_parser(
        "install",
        help="copy skills into a skills directory",
        description=(
            "Copy skill directories into DIR (one directory per skill). Claude Code reads "
            ".claude/skills; Codex and other Agent Skills clients read .agents/skills."
        ),
    )
    inst.add_argument("names", nargs="*", metavar="NAME", help="skills to install (default: all)")
    inst.add_argument(
        "--dir",
        default=DEFAULT_DIR,
        help=f"skills directory (default: {DEFAULT_DIR}; use .agents/skills for Codex)",
    )
    inst.add_argument("--force", action="store_true", help="replace skills that are already installed")
    inst.set_defaults(handler=_cmd_install)
