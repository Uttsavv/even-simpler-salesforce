"""Multi-process refresh-token rotation driver (§19 acceptance criterion).

"With rotation enabled in a sandbox, no ``invalid_grant`` across 1,000 refresh cycles from 4
processes sharing a CAS store."

:func:`run_rotation` starts N worker processes (``spawn`` context, so nothing is inherited by
fork). Every worker builds its own client (async or sync) over the same
:class:`~tests.sandbox.sqlite_store.SqliteTokenStore` file and ``token_key``, waits on a shared
barrier, then keeps forcing refreshes by calling the runtime's ``refresh()`` with the token it
currently holds (exactly what a 401 does) until the shared chain has rotated ``rotations``
times in total (the store's version is the counter: one successful refresh = one CAS save).
A worker whose refresh was beaten by another process adopts the stored token instead of
POSTing; that is the algorithm working, and it is counted separately. Each worker reports what
happened; the caller asserts on the reports.

Used by ``tests/sandbox/test_rotation.py`` (real org) and ``tests/contract/test_rotation_multiprocess.py``
(respx fake token endpoint with rotation semantics, runs in normal CI). In the mocked variant
``install`` names a ``module:function`` that each worker enters as a context manager before it
creates its client (it starts respx in that process).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import multiprocessing
import time
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

import even_simpler_salesforce as esf

from .sqlite_store import AsyncSqliteTokenStore, SqliteTokenStore

__all__ = ["RotationConfig", "WorkerReport", "run_rotation", "worker"]


@dataclass(frozen=True)
class RotationConfig:
    """Everything a worker needs (picklable). Secrets are passed to children, never printed."""

    db_path: str
    token_key: str
    domain: str
    client_id: str
    redirect_uri: str
    client_secret: str | None = field(default=None, repr=False)
    rotations: int = 1000  # stop when the shared chain has rotated this often in total
    base_version: int = 0  # store version of ``token_key`` before the run
    max_attempts: int = 0  # per-worker cap on refresh() calls (0 = 4 * rotations)
    kind: str = "sync"  # "async" | "sync"
    verify_every: int = 25  # call userinfo after every N-th refresh (0 = never)
    pause: float = 0.0  # seconds to sleep between cycles
    install: str | None = None  # "module:function" -> context manager, entered in the child
    install_arg: str | None = None
    api_version: str | None = None


@dataclass
class WorkerReport:
    index: int
    kind: str
    cycles_done: int = 0  # refresh() calls that returned a token
    posts: int = 0  # refreshes this worker actually sent (``sf.auth.refreshed`` events)
    rotated: int = 0  # of those, how many returned a new refresh token
    adopted: int = 0  # refresh() calls answered by a token another process had saved
    verified: int = 0  # successful userinfo calls
    reauth_required: int = 0  # ReauthorizationRequired (invalid_grant) - must stay 0
    errors: list[str] = field(default_factory=lambda: [])
    seconds: float = 0.0


class _Counter:
    def __init__(self, report: WorkerReport) -> None:
        self.report = report

    def on_event(self, event: esf.Event) -> None:
        if event.name == "sf.auth.refreshed" and not event.get("initial"):
            self.report.posts += 1
            if event.get("rotated"):
                self.report.rotated += 1


def _resolve(ref: str) -> Callable[..., Any]:
    module, _, name = ref.partition(":")
    return getattr(importlib.import_module(module), name)


def _flow(cfg: RotationConfig) -> esf.WebServerFlow:
    return esf.WebServerFlow(
        domain=cfg.domain,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        redirect_uri=cfg.redirect_uri,
        token_key=cfg.token_key,
    )


def _client_kwargs(cfg: RotationConfig, report: WorkerReport) -> dict[str, Any]:
    kw: dict[str, Any] = {"observers": [_Counter(report)], "client_name": "esf-sandbox-rotation"}
    if cfg.api_version:
        kw["api_version"] = cfg.api_version
    return kw


def _describe(exc: BaseException) -> str:
    # Library exception messages are redacted; keep the type, code and first line only.
    code = getattr(exc, "error_code", None)
    return f"{type(exc).__name__}({code}): {str(exc).splitlines()[0][:200] if str(exc) else ''}"


def _done(cfg: RotationConfig, store: SqliteTokenStore) -> bool:
    cur = store.load(cfg.token_key)
    return cur is not None and cur.version - cfg.base_version >= cfg.rotations


def _attempts(cfg: RotationConfig) -> int:
    return cfg.max_attempts or 4 * cfg.rotations


def _after_refresh(report: WorkerReport, held: esf.Token, new: esf.Token, posts_before: int) -> None:
    report.cycles_done += 1
    if report.posts == posts_before and new.access_token != held.access_token:
        report.adopted += 1


def _sync_loop(cfg: RotationConfig, report: WorkerReport, barrier: Any) -> None:
    store = SqliteTokenStore(cfg.db_path)
    sf = esf.Salesforce(_flow(cfg), store=store, **_client_kwargs(cfg, report))
    try:
        rt: Any = sf._runtime  # pyright: ignore[reportPrivateUsage]
        rt.token()
        barrier.wait()
        for _ in range(_attempts(cfg)):
            if _done(cfg, store):
                break
            held = rt.current() or rt.token()
            posts_before = report.posts
            try:
                new = rt.refresh(held)
            except esf.ReauthorizationRequired as exc:
                report.reauth_required += 1
                report.errors.append(_describe(exc))
                return
            except esf.SalesforceError as exc:
                report.errors.append(_describe(exc))
                continue
            _after_refresh(report, held, new, posts_before)
            if cfg.verify_every and report.cycles_done % cfg.verify_every == 0:
                sf.auth.userinfo()
                report.verified += 1
            if cfg.pause:
                time.sleep(cfg.pause)
    finally:
        sf.close()


async def _async_loop(cfg: RotationConfig, report: WorkerReport, barrier: Any) -> None:
    store = AsyncSqliteTokenStore(cfg.db_path)
    sf = esf.AsyncSalesforce(_flow(cfg), store=store, **_client_kwargs(cfg, report))
    try:
        rt: Any = sf._runtime  # pyright: ignore[reportPrivateUsage]
        await rt.token()
        await asyncio.to_thread(barrier.wait)
        for _ in range(_attempts(cfg)):
            if _done(cfg, store.sync):
                break
            held = rt.current() or await rt.token()
            posts_before = report.posts
            try:
                new = await rt.refresh(held)
            except esf.ReauthorizationRequired as exc:
                report.reauth_required += 1
                report.errors.append(_describe(exc))
                return
            except esf.SalesforceError as exc:
                report.errors.append(_describe(exc))
                continue
            _after_refresh(report, held, new, posts_before)
            if cfg.verify_every and report.cycles_done % cfg.verify_every == 0:
                await sf.auth.userinfo()
                report.verified += 1
            if cfg.pause:
                await asyncio.sleep(cfg.pause)
    finally:
        await sf.aclose()


def worker(cfg: RotationConfig, index: int, barrier: Any) -> WorkerReport:
    """Run one worker (in a child process). Never raises: failures go into the report."""
    report = WorkerReport(index=index, kind=cfg.kind)
    started = time.perf_counter()
    with contextlib.ExitStack() as stack:
        try:
            if cfg.install:
                stack.enter_context(_resolve(cfg.install)(cfg.install_arg))
            if cfg.kind == "async":
                asyncio.run(_async_loop(cfg, report, barrier))
            else:
                _sync_loop(cfg, report, barrier)
        except BaseException as exc:  # report, don't crash the pool
            report.errors.append(_describe(exc))
            report.errors.append("".join(traceback.format_exception_only(type(exc), exc)).strip()[:500])
            with contextlib.suppress(Exception):
                barrier.abort()
    report.seconds = round(time.perf_counter() - started, 2)
    return report


def run_rotation(
    cfg: RotationConfig, kinds: Sequence[str] = ("async", "sync", "async", "sync"), *, timeout: float = 3600
) -> list[WorkerReport]:
    """Run ``len(kinds)`` workers in separate processes; return their reports (in order).

    ``cfg.base_version`` is filled in from the store before the workers start.
    """
    current = SqliteTokenStore(cfg.db_path).load(cfg.token_key)
    cfg = replace(cfg, base_version=current.version if current is not None else 0)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as manager:
        barrier: Any = manager.Barrier(len(kinds), timeout=timeout)
        with ProcessPoolExecutor(max_workers=len(kinds), mp_context=ctx) as pool:
            futures = [
                pool.submit(worker, replace(cfg, kind=kind), i, barrier) for i, kind in enumerate(kinds)
            ]
            return [f.result(timeout=timeout) for f in futures]


def summarise(reports: Sequence[WorkerReport]) -> str:
    lines = [
        f"  worker {r.index} ({r.kind}): cycles={r.cycles_done} posts={r.posts} rotated={r.rotated} "
        f"adopted={r.adopted} verified={r.verified} reauth={r.reauth_required} "
        f"errors={len(r.errors)} {r.seconds}s"
        for r in reports
    ]
    for r in reports:
        lines.extend(f"    worker {r.index}: {e}" for e in r.errors[:5])
    return "\n".join(lines)
