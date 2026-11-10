"""``esf generate actions``: write typed stubs for invocable actions (§9.7, §9.18)."""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from ..core.actions import ACTION_KINDS, check_action_name
from ._actions_codegen import ActionDescribe, render_actions
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
        source = render_actions(
            describes,
            org=str(sf.instance_url or ""),
            api_version=sf.api_version,
            generated_at=datetime.now(timezone.utc),
            pydantic=args.pydantic,
        )
    out = Path(args.out)
    out.write_text(source, encoding="utf-8")
    noun = "action" if len(describes) == 1 else "actions"
    err.write(f"wrote {len(describes)} {noun} to {out}\n")
    return 0
