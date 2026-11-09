"""Async primitives. ``_sync/_compat.py`` is the hand-written twin (not generated)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

__all__ = ["Lock", "Periodic", "Semaphore", "gather_limited", "nullcontext", "sleep"]

T = TypeVar("T")

Lock = asyncio.Lock
Semaphore = asyncio.Semaphore
sleep = asyncio.sleep


def nullcontext() -> contextlib.AbstractAsyncContextManager[None]:
    return contextlib.nullcontext()


async def gather_limited(n: int, funcs: Sequence[Callable[[], Awaitable[T]]]) -> list[T]:
    """Run ``funcs`` with at most ``n`` in flight; results keep the input order."""
    if n <= 1 or len(funcs) <= 1:
        return [await f() for f in funcs]
    sem = asyncio.Semaphore(n)

    async def run(f: Callable[[], Awaitable[T]]) -> T:
        async with sem:
            return await f()

    return list(await asyncio.gather(*(run(f) for f in funcs)))


class Periodic:
    """Calls ``fn`` every ``interval`` seconds in a background task until stopped."""

    def __init__(self, fn: Callable[[], Awaitable[Any]], interval: float) -> None:
        self._fn = fn
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self._fn()
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
