"""Async primitives. ``_sync/_compat.py`` is the hand-written twin (not generated)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, TypeVar

__all__ = [
    "GrpcStream",
    "Lock",
    "Periodic",
    "Semaphore",
    "gather_settled",
    "grpc_channel",
    "grpc_close",
    "grpc_unary",
    "iter_bytes",
    "nullcontext",
    "sleep",
    "to_thread",
]

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


def iter_bytes(response: Any) -> AsyncIterator[bytes]:
    """Decoded body chunks of a streamed ``httpx.Response``."""
    return response.aiter_bytes()


async def to_thread(fn: Callable[..., T], *args: Any) -> T:
    """Run blocking ``fn`` (a file read) off the event loop."""
    return await asyncio.to_thread(fn, *args)


# --- gRPC (Pub/Sub API, §5.4) ---------------------------------------------------------------
# grpc is imported on first use: it is an optional extra, and this module is imported by every
# client. Only this module and pubsub_transport talk to grpc (spec §5.1).

_LOOPBACK = ("localhost:", "127.0.0.1:", "[::1]:")


def _grpc_aio() -> Any:
    from ..core.pubsub import missing_extra

    try:
        import grpc
        import grpc.aio  # pyright: ignore[reportUnusedImport]
    except ImportError as exc:
        raise missing_extra(exc) from None
    return grpc


def _failure(exc: Any) -> Any:
    """``grpc.aio.AioRpcError`` -> :class:`~..core.pubsub.RpcFailure`."""
    from ..core.pubsub import RpcFailure

    trailers = {
        str(k).lower(): v.decode() if isinstance(v, bytes) else str(v)
        for k, v in exc.trailing_metadata() or ()
    }
    return RpcFailure(exc.code().name, exc.details() or "", trailers)


def grpc_channel(target: str) -> Any:
    """A TLS channel to ``target``; a plaintext one for loopback test servers only."""
    grpc = _grpc_aio()
    options = [("grpc.max_receive_message_length", 64 * 1024 * 1024)]
    if target.startswith(_LOOPBACK):
        return grpc.aio.insecure_channel(target, options=options)
    return grpc.aio.secure_channel(target, grpc.ssl_channel_credentials(), options=options)


async def grpc_close(channel: Any) -> None:
    await channel.close()


async def grpc_unary(
    channel: Any,
    method: str,
    request: Any,
    *,
    metadata: Sequence[tuple[str, str]],
    response_type: Any,
    timeout: float,
) -> Any:
    """One unary call; failures raise ``RpcFailure``."""
    grpc = _grpc_aio()
    request_type: Any = request.__class__
    call = channel.unary_unary(
        method,
        request_serializer=request_type.SerializeToString,
        response_deserializer=response_type.FromString,
    )
    try:
        return await call(request, metadata=tuple(metadata), timeout=timeout)
    except grpc.aio.AioRpcError as exc:
        raise _failure(exc) from None


class GrpcStream:
    """A bidirectional gRPC stream: ``write()`` requests, ``read()`` responses.

    ``read()`` returns ``None`` when the server ends the stream cleanly; failures raise
    ``RpcFailure``.
    """

    def __init__(
        self,
        channel: Any,
        method: str,
        *,
        metadata: Sequence[tuple[str, str]],
        request_type: Any,
        response_type: Any,
    ) -> None:
        self._grpc = _grpc_aio()
        factory = channel.stream_stream(
            method,
            request_serializer=request_type.SerializeToString,
            response_deserializer=response_type.FromString,
        )
        self._call = factory(metadata=tuple(metadata))

    async def _status(self) -> Any:
        from ..core.pubsub import RpcFailure

        code = await self._call.code()
        trailers = await self._call.trailing_metadata()
        details = await self._call.details()
        return RpcFailure(code.name, details or "", {str(k).lower(): str(v) for k, v in trailers or ()})

    async def write(self, message: Any) -> None:
        try:
            await self._call.write(message)
        except self._grpc.aio.AioRpcError as exc:
            raise _failure(exc) from None
        except Exception:  # the call already finished (e.g. closed by the server)
            raise await self._status() from None

    async def read(self) -> Any:
        try:
            msg = await self._call.read()
        except self._grpc.aio.AioRpcError as exc:
            raise _failure(exc) from None
        if msg is self._grpc.aio.EOF:
            failure = await self._status()
            if failure.status != "OK":
                raise failure
            return None
        return msg

    def cancel(self) -> None:
        self._call.cancel()
