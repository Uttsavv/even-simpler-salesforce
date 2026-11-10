"""FastAPI integration (§13.1, ``[fastapi]`` extra).

    from even_simpler_salesforce.contrib.fastapi import SalesforceIntegration

    esf_app = SalesforceIntegration(esf.ClientCredentials(...), error_handlers=True)
    app = FastAPI(lifespan=esf_app.lifespan)
    esf_app.init_app(app)

    SF = Annotated[esf.AsyncSalesforce, Depends(esf_app.admin)]

One :class:`~even_simpler_salesforce.AsyncSalesforce` per process, created when the app
starts and closed when it stops. Routes get it, or a per-user child, through dependencies.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .._async.client import AsyncSalesforce
from ..observability.summary import capture
from ._http import HANDLED, http_error

if TYPE_CHECKING:
    from ..auth import AsyncTokenStore, Auth
    from ..testing import AsyncFakeSalesforce

__all__ = ["SalesforceIntegration"]

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class SalesforceIntegration:
    """Client lifecycle, dependencies, per-request roll-up and error handlers for FastAPI.

    Example:
        esf_app = SalesforceIntegration(esf.ClientCredentials(...))
        app = FastAPI(lifespan=esf_app.lifespan)
        esf_app.init_app(app)

        @app.get("/leads/{lead_id}")
        async def get_lead(lead_id: str, sf: Annotated[esf.AsyncSalesforce, Depends(esf_app.admin)]):
            return await sf.sobject("Lead").get(lead_id)

    Args:
        auth: Login flow of the app-wide client. The integration builds
            ``AsyncSalesforce(auth, store=store)`` at startup. Give ``auth`` or ``client``.
        client: A client you built yourself (to set ``cursor_key=``, ``observers=``, ...), or a
            fake in tests. The integration closes it at shutdown.
        store: Token store of the app-wide client (with ``auth``) and of the per-user clients.
            Defaults to the app-wide client's store.
        auth_for: Returns the login flow of the user behind a request (sync or async), for the
            :meth:`user` dependency. Usually a ``WebServerFlow`` with a per-user ``token_key``.
        on_summary: Called with the request and the ``esf.capture()`` roll-up fields
            (``sf_calls``, ``sf_ms``, ...) after every HTTP request.
        error_handlers: Map typed errors to JSON responses (see the FastAPI guide for the
            table). Off by default.
    """

    def __init__(
        self,
        auth: Auth | None = None,
        *,
        client: AsyncSalesforce | AsyncFakeSalesforce | None = None,
        store: AsyncTokenStore | None = None,
        auth_for: Callable[[Request], Auth | Awaitable[Auth]] | None = None,
        on_summary: Callable[[Request, dict[str, Any]], None] | None = None,
        error_handlers: bool = False,
    ) -> None:
        if (auth is None) == (client is None):
            raise TypeError("SalesforceIntegration needs exactly one of auth= or client=")
        self._auth = auth
        self._given = client
        self._store = store
        self._auth_for = auth_for
        self._on_summary = on_summary
        self._error_handlers = error_handlers
        self._client: AsyncSalesforce | None = None
        self._installed = False

    # --- setup -----------------------------------------------------------------------

    def init_app(self, app: FastAPI) -> None:
        """Add the roll-up middleware (with ``on_summary``) and the error handlers to ``app``.

        Call it before the app starts: Starlette builds its middleware stack on the first
        request or lifespan event and refuses changes after that.
        """
        if self._on_summary is not None:
            app.add_middleware(_SummaryMiddleware, on_summary=self._on_summary)
        if self._error_handlers:
            for cls in HANDLED:
                app.add_exception_handler(cls, _handle_error)
        self._installed = True

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        """Pass as ``FastAPI(lifespan=...)``: creates the client at startup and closes it at shutdown.

        To run your own startup code too, enter it from your lifespan:
        ``async with esf_app.lifespan(app): yield``.

        Raises:
            RuntimeError: ``on_summary`` or ``error_handlers`` is set but :meth:`init_app`
                was not called.
        """
        if not self._installed and (self._on_summary is not None or self._error_handlers):
            raise RuntimeError("call esf_app.init_app(app) before the app starts")
        if self._given is not None:
            client = cast(AsyncSalesforce, self._given)
        else:
            assert self._auth is not None
            client = AsyncSalesforce(self._auth, store=self._store)
        self._client = client
        try:
            yield
        finally:
            self._client = None
            await client.aclose()

    # --- dependencies ----------------------------------------------------------------

    @property
    def client(self) -> AsyncSalesforce:
        """The app-wide client, for code outside a route (background tasks, startup hooks).

        Raises:
            RuntimeError: The app is not running (lifespan not entered).
        """
        if self._client is None:
            raise RuntimeError("the Salesforce client exists only while the app's lifespan is running")
        return self._client

    def admin(self) -> AsyncSalesforce:
        """Dependency: the app-wide client (``Depends(esf_app.admin)``)."""
        return self.client

    async def user(self, request: Request) -> AsyncSalesforce:
        """Dependency: a child of the app-wide client logged in as the request's user.

        Built with ``client.with_auth(auth_for(request), store=store)``: it shares the
        connection pool, caches and usage counters, and loads the user's token from the store.

        Raises:
            RuntimeError: No ``auth_for`` was configured.
        """
        if self._auth_for is None:
            raise RuntimeError("SalesforceIntegration(auth_for=...) is needed for the user dependency")
        auth = self._auth_for(request)
        if inspect.isawaitable(auth):
            auth = await auth
        return self.client.with_auth(auth, store=self._store)


class _SummaryMiddleware:
    """Pure ASGI middleware: one ``esf.capture()`` per HTTP request, reported to ``on_summary``."""

    def __init__(self, app: ASGIApp, on_summary: Callable[[Request, dict[str, Any]], None]) -> None:
        self.app = app
        self.on_summary = on_summary

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with capture() as summary:
            try:
                await self.app(scope, receive, send)
            finally:
                self.on_summary(Request(scope), summary.as_fields())


async def _handle_error(request: Request, exc: Exception) -> JSONResponse:
    mapped = http_error(exc)
    if mapped is None:
        raise exc  # not ours to map (e.g. bad SOQL): Starlette logs it and answers 500
    return JSONResponse(mapped.body, status_code=mapped.status, headers=mapped.headers)
