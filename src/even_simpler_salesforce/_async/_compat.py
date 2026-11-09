"""Async primitives. ``_sync/_compat.py`` is the hand-written twin (not generated)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

__all__ = ["Lock", "Periodic", "Semaphore", "gather_settled", "nullcontext", "sleep"]

T = TypeVar("T")

Lock = asyncio.Lock
Semaphore = asyncio.Semaphore
sleep = asyncio.sleep


def nullcontext() -> contextlib.AbstractAsyncContextManager[None]:
    return contextlib.nullcontext()


async def gather_settled(n: int, funcs: Sequence[Callable[[], Awaitable[T]]]) -> list[T | Exception]:
    """Run ``funcs`` with at most ``n`` in flight; results keep the input order.

    Always waits for every call and returns exceptions in place of results, so nothing is left
    running in the background when this returns."""
    if n <= 1 or len(funcs) <= 1:
        out: list[T | Exception] = []
        for f in funcs:
            try:
                out.append(await f())
            except Exception as exc:
                out.append(exc)
        return out
    sem = asyncio.Semaphore(n)

    async def run(f: Callable[[], Awaitable[T]]) -> T:
        async with sem:
            return await f()

    settled = await asyncio.gather(*(run(f) for f in funcs), return_exceptions=True)
    for item in settled:
        if isinstance(item, BaseException) and not isinstance(item, Exception):
            raise item  # cancellation etc. must propagate
    return [item for item in settled if not isinstance(item, BaseException) or isinstance(item, Exception)]  # pyright: ignore[reportReturnType]


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
