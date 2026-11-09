"""Caller tags and priority carried by a contextvar (§10.2)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

from .config import Priority

__all__ = ["CallContext", "call_context", "current_call_context"]


@dataclass(frozen=True, slots=True)
class CallContext:
    caller: str | None = None
    priority: Priority | None = None


_CTX: ContextVar[CallContext] = ContextVar("esf_call_context", default=CallContext())  # noqa: B039 - frozen


def current_call_context() -> CallContext:
    return _CTX.get()


@contextlib.contextmanager
def call_context(*, caller: str | None = None, priority: Priority | None = None) -> Iterator[CallContext]:
    """Tag every Salesforce call made inside the block (works across awaits).

    Example:
        with esf.call_context(caller="route:/leads", priority="high"):
            await sf.query(...)
    """
    outer = _CTX.get()
    ctx = CallContext(caller=caller or outer.caller, priority=priority or outer.priority)
    token = _CTX.set(ctx)
    try:
        yield ctx
    finally:
        _CTX.reset(token)
