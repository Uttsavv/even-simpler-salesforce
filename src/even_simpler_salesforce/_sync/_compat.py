"""Sync primitives. Hand-written twin of ``_async/_compat.py`` (NOT generated)."""

from __future__ import annotations

import contextlib
import contextvars
import queue
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
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


def iter_bytes(response: Any) -> Iterator[bytes]:
    """Decoded body chunks of a streamed ``httpx.Response``."""
    return response.iter_bytes()


def to_thread(fn: Callable[..., T], *args: Any) -> T:
    """Run ``fn`` in the calling thread (the sync twin of ``asyncio.to_thread``)."""
    return fn(*args)


# --- gRPC (Pub/Sub API, §5.4) ---------------------------------------------------------------
# The sync twin of _async/_compat.py's gRPC helpers, over the blocking grpc API. grpc is
# imported on first use: it is an optional extra.

_LOOPBACK = ("localhost:", "127.0.0.1:", "[::1]:")
_END = object()


def _grpc() -> Any:
    from ..core.pubsub import missing_extra

    try:
        import grpc
    except ImportError as exc:
        raise missing_extra(exc) from None
    return grpc


def _failure(exc: Any) -> Any:
    """``grpc.RpcError`` (also a ``grpc.Call``) -> :class:`~..core.pubsub.RpcFailure`."""
    from ..core.pubsub import RpcFailure

    code = exc.code() if callable(getattr(exc, "code", None)) else None
    if code is None:  # pragma: no cover - every RpcError raised by grpc is a Call
        return RpcFailure("UNKNOWN", str(exc))
    trailers = {
        str(k).lower(): v.decode() if isinstance(v, bytes) else str(v)
        for k, v in exc.trailing_metadata() or ()
    }
    return RpcFailure(code.name, exc.details() or "", trailers)


def grpc_channel(target: str) -> Any:
    """A TLS channel to ``target``; a plaintext one for loopback test servers only."""
    grpc = _grpc()
    options = [("grpc.max_receive_message_length", 64 * 1024 * 1024)]
    if target.startswith(_LOOPBACK):
        return grpc.insecure_channel(target, options=options)
    return grpc.secure_channel(target, grpc.ssl_channel_credentials(), options=options)


def grpc_close(channel: Any) -> None:
    channel.close()


def grpc_unary(
    channel: Any,
    method: str,
    request: Any,
    *,
    metadata: Sequence[tuple[str, str]],
    response_type: Any,
    timeout: float,
) -> Any:
    """One unary call; failures raise ``RpcFailure``."""
    grpc = _grpc()
    request_type: Any = request.__class__
    call = channel.unary_unary(
        method,
        request_serializer=request_type.SerializeToString,
        response_deserializer=response_type.FromString,
    )
    try:
        return call(request, metadata=tuple(metadata), timeout=timeout)
    except grpc.RpcError as exc:
        raise _failure(exc) from None


class GrpcStream:
    """A bidirectional gRPC stream over the blocking API.

    Requests go through a queue that feeds grpc's request iterator; ``read()`` blocks on the
    response iterator. ``read()`` returns ``None`` when the server ends the stream cleanly;
    failures raise ``RpcFailure``.
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
        self._grpc = _grpc()
        self._requests: queue.SimpleQueue[Any] = queue.SimpleQueue()
        factory = channel.stream_stream(
            method,
            request_serializer=request_type.SerializeToString,
            response_deserializer=response_type.FromString,
        )
        self._responses: Any = factory(iter(self._requests.get, _END), metadata=tuple(metadata))

    def write(self, message: Any) -> None:
        self._requests.put(message)

    def read(self) -> Any:
        try:
            return next(self._responses)
        except StopIteration:
            return None
        except self._grpc.RpcError as exc:
            raise _failure(exc) from None

    def cancel(self) -> None:
        self._requests.put(_END)
        self._responses.cancel()
