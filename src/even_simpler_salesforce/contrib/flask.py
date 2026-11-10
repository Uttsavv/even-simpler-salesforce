"""Flask integration (§13.2, ``[flask]`` extra).

    from even_simpler_salesforce.contrib.flask import Salesforce

    sf_ext = Salesforce(auth=esf.ClientCredentials(...), error_handlers=True)

    def create_app():
        app = Flask(__name__)
        sf_ext.init_app(app)
        return app

    # in a view:
    rows = sf_ext.client.query(soql).records

One sync :class:`~even_simpler_salesforce.Salesforce` client per app and process, shared by
every thread, closed when the process exits.
"""

from __future__ import annotations

import atexit
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from flask import Flask, Request, Response, current_app, g, jsonify, request

from .._sync.client import Salesforce as SyncSalesforce
from ..observability.summary import Summary, capture
from ._http import HANDLED, http_error

if TYPE_CHECKING:
    from ..auth import Auth, TokenStore
    from ..testing import FakeSalesforce

__all__ = ["Salesforce"]

EXTENSION = "even_simpler_salesforce"
"""Key of the client in ``app.extensions``."""

_G_KEY = "_esf_capture"


class Salesforce:
    """Flask extension: one shared client, a per-request roll-up and error handlers.

    Supports the app factory pattern: create it once, then call :meth:`init_app` for each app.
    Each app gets its own client; nothing app-specific is kept on the extension.

    Example:
        sf_ext = Salesforce(auth=esf.ClientCredentials(...))
        sf_ext.init_app(app)

        @app.get("/leads/<lead_id>")
        def get_lead(lead_id):
            return sf_ext.client.sobject("Lead").get(lead_id)

    Args:
        app: Initialise this app now (same as calling :meth:`init_app`).
        auth: Login flow. Each :meth:`init_app` builds ``Salesforce(auth, store=store)``.
            Give ``auth`` or ``client``.
        client: A client you built yourself (to set ``cursor_key=``, ``observers=``, ...), or a
            fake in tests. Closed when the process exits.
        store: Token store of the client built from ``auth``.
        on_summary: Called with the request and the ``esf.capture()`` roll-up fields
            (``sf_calls``, ``sf_ms``, ...) when each request is torn down.
        error_handlers: Map typed errors to JSON responses (see the Flask guide for the
            table). Off by default.
    """

    def __init__(
        self,
        app: Flask | None = None,
        *,
        auth: Auth | None = None,
        client: SyncSalesforce | FakeSalesforce | None = None,
        store: TokenStore | None = None,
        on_summary: Callable[[Request, dict[str, Any]], None] | None = None,
        error_handlers: bool = False,
    ) -> None:
        if (auth is None) == (client is None):
            raise TypeError("Salesforce extension needs exactly one of auth= or client=")
        if client is not None and store is not None:
            raise TypeError("store= configures the client built from auth=; set it on your client instead")
        self._auth = auth
        self._given = client
        self._store = store
        self._on_summary = on_summary
        self._error_handlers = error_handlers
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        """Create the app's client and register the request hooks and error handlers."""
        if self._given is not None:
            client = cast(SyncSalesforce, self._given)
        else:
            assert self._auth is not None
            client = SyncSalesforce(self._auth, store=self._store)
        atexit.register(client.close)
        app.extensions[EXTENSION] = client
        if self._on_summary is not None:
            app.before_request(_start_capture)
            app.teardown_request(self._report)
        if self._error_handlers:
            for cls in HANDLED:
                app.register_error_handler(cls, _handle_error)

    @property
    def client(self) -> SyncSalesforce:
        """The current app's client. Needs an app context (any view, CLI command or test).

        Raises:
            RuntimeError: Outside an app context, or :meth:`init_app` was not called for the app.
        """
        try:
            return current_app.extensions[EXTENSION]
        except KeyError:
            raise RuntimeError("Salesforce.init_app(app) was not called for this app") from None

    def _report(self, exc: BaseException | None) -> None:
        state: tuple[contextlib.ExitStack, Summary] | None = g.pop(_G_KEY, None)
        if state is None:  # an earlier before_request hook ended the request before ours ran
            return
        stack, summary = state
        stack.close()
        assert self._on_summary is not None
        self._on_summary(request, summary.as_fields())


def _start_capture() -> None:
    stack = contextlib.ExitStack()
    summary = stack.enter_context(capture())
    g.setdefault(_G_KEY, (stack, summary))


def _handle_error(exc: Exception) -> tuple[Response, int, dict[str, str]]:
    mapped = http_error(exc)
    if mapped is None:
        raise exc  # not ours to map (e.g. bad SOQL): Flask logs it and answers 500
    return jsonify(mapped.body), mapped.status, mapped.headers
