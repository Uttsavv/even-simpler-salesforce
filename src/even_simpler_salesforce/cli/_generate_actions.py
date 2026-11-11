"""``esf generate actions``: write typed stubs for invocable actions (§9.7, §9.18)."""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import re
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from ..core.actions import ACTION_KINDS, check_action_name
from ._actions_codegen import ActionDescribe, render_actions
from ._codegen import file_header, write_output
from ._env import EnvConfigError

__all__ = ["env_prefix", "generate_actions", "parse_include"]

_ORG = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_WILDCARD = re.compile(r"[*?\[]")


def env_prefix(org: str | None) -> str:
    """``None`` → ``ESF_``; ``"sandbox"`` → ``ESF_SANDBOX_``."""
    if org is None:
        return "ESF_"
    if not _ORG.fullmatch(org):
        raise EnvConfigError(f"--org must be letters, digits or '_', got {org!r}")
    return f"ESF_{org.upper()}_"


def parse_include(value: str) -> tuple[str, str]:
    """``"flow:Assign_Lead"`` → ``("flow", "Assign_Lead")``; the name may be a glob (``apex:*``)."""
    kind, sep, name = value.partition(":")
    if not sep or kind not in ACTION_KINDS or not name:
        kinds = ", ".join(ACTION_KINDS)
        raise ValueError(f"--include must look like KIND:NAME with KIND one of {kinds}, got {value!r}")
    if not _WILDCARD.search(name):
        check_action_name(name)
    return kind, name


def _select(sf: Any, includes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    chosen: dict[tuple[str, str], None] = {}
    listed: dict[str, list[str]] = {}
    for kind, pattern in includes:
        if not _WILDCARD.search(pattern):
            chosen[(kind, pattern)] = None
            continue
        if kind not in listed:
            listed[kind] = [str(a.get("name")) for a in sf.actions.list(kind) if a.get("name")]
        matched = [n for n in listed[kind] if fnmatch.fnmatchcase(n, pattern)]
        if not matched:
            raise ValueError(f"no {kind} actions match {pattern!r}")
        chosen.update(dict.fromkeys((kind, n) for n in matched))
    return list(chosen)


def generate_actions(
    args: argparse.Namespace,
    env: Mapping[str, str],
    err: TextIO,
    client: Callable[[Mapping[str, str], str], Any],
) -> int:
    """Run ``esf generate actions``; returns the exit code (errors propagate to ``main``)."""
    if args.pydantic and importlib.util.find_spec("pydantic") is None:
        raise EnvConfigError("--pydantic needs pydantic: pip install 'even-simpler-salesforce[pydantic]'")
    includes = [parse_include(v) for v in args.include]
    prefix = env_prefix(args.org)
    with client(env, prefix) as sf:
        selected = _select(sf, includes)
        describes = [ActionDescribe(k, n, sf.actions.describe(k, n)) for k, n in selected]
        header = file_header(
            "Typed invocable action stubs",
            command=["generate", "actions"]
            + [x for v in args.include for x in ("--include", v)]
            + ["--out", args.out]
            + (["--pydantic"] if args.pydantic else [])
            + (["--org", args.org] if args.org else []),
            instance_url=sf.instance_url,
            org_id=sf.auth.token().org_id,
            api_version=sf.api_version,
        )
        source = render_actions(describes, header=header, pydantic=args.pydantic)
    # write_output turns an unwritable path into a usage error (exit 2), like the other targets.
    write_output(source, args.out, err, err)
    noun = "action" if len(describes) == 1 else "actions"
    err.write(f"generated {len(describes)} {noun}\n")
    return 0


def register(targets: Any) -> None:
    """Add ``actions`` to the ``esf generate`` subparsers."""
    p = targets.add_parser("actions", help="typed stubs for invocable actions (sf.run)")
    p.add_argument(
        "--include",
        action="append",
        required=True,
        metavar="KIND:NAME",
        help="action to include, e.g. flow:Assign_Lead or apex:* (repeatable; kinds: standard, flow, apex)",
    )
    p.add_argument("--out", required=True, help="Python file to write")
    p.add_argument(
        "--pydantic", action="store_true", help="emit Pydantic models (needs the [pydantic] extra)"
    )
    p.add_argument("--org", default=None, help="read the login from ESF_<ORG>_* instead of ESF_*")
    p.set_defaults(handler=_run)


def _run(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    from ._env import client_from_env

    return generate_actions(args, env, err, client_from_env)
