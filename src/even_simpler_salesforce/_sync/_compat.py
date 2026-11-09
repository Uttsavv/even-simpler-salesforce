"""Sync primitives. Hand-written twin of ``_async/_compat.py`` (NOT generated)."""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

__all__ = ["Lock", "Periodic", "Semaphore", "gather_settled", "nullcontext", "sleep"]

T = TypeVar("T")

Lock = threading.Lock
Semaphore = threading.BoundedSemaphore
sleep = time.sleep


def nullcontext() -> contextlib.AbstractContextManager[None]:
    return contextlib.nullcontext()


def gather_settled(n: int, funcs: Sequence[Callable[[], T]]) -> list[T | Exception]:
    """Run ``funcs`` on up to ``n`` threads; results keep the input order.

    Always waits for every call and returns exceptions in place of results. Each task runs in a
    copy of the caller's context so ``capture()`` and ``call_context()`` keep working."""
    if n <= 1 or len(funcs) <= 1:
        out: list[T | Exception] = []
        for f in funcs:
            try:
                out.append(f())
            except Exception as exc:
                out.append(exc)
        return out
    with ThreadPoolExecutor(max_workers=min(n, len(funcs))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, f) for f in funcs]
        results: list[T | Exception] = []
        for fut in futures:
            exc = fut.exception()
            if exc is None:
                results.append(fut.result())
            elif isinstance(exc, Exception):
                results.append(exc)
            else:  # pragma: no cover - BaseException in a worker thread
                raise exc
        return results


class Periodic:
    """Calls ``fn`` every ``interval`` seconds on a daemon thread until stopped."""

    def __init__(self, fn: Callable[[], Any], interval: float) -> None:
        self._fn = fn
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._stop = threading.Event()  # fresh event so a stopped poller can restart
            self._thread = threading.Thread(target=self._run, name="esf-limits-poller", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                self._fn()
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
