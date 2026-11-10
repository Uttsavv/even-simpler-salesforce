"""``esf generate <target>``: code generated from an org (§9.18).

Each target is one module with a ``register(targets)`` function. ``register`` adds its
subparser and sets ``handler`` (a :data:`._codegen.Handler`) with ``set_defaults``; ``esf``
then calls ``handler(args, env, out, err)`` and returns its exit code. To add a target, write
the module and append its ``register`` to :data:`TARGETS`. Generated files start with
:func:`._codegen.file_header`, so every target names the org, API version and time the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import _generate_actions, _models

__all__ = ["TARGETS", "add_generate_parser"]

#: ``register(targets)`` for each ``esf generate`` target, in ``--help`` order.
TARGETS: list[Callable[[Any], None]] = [_models.register, _generate_actions.register]


def add_generate_parser(sub: Any) -> None:
    """Add ``esf generate`` and its targets to the top-level subparsers ``sub``."""
    g = sub.add_parser("generate", help="generate typed code from the org's describe data")
    targets = g.add_subparsers(dest="target", metavar="TARGET")
    targets.required = True
    for register in TARGETS:
        register(targets)
