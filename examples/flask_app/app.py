"""Sample Flask app using only even-simpler-salesforce's public API.

Run it (from the ``examples/`` directory)::

    export SF_DOMAIN=https://acme.my.salesforce.com SF_CLIENT_ID=... SF_CLIENT_SECRET=...
    # Signs pagination cursors. Generate it once, keep it secret, share it across workers:
    export SF_CURSOR_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    flask --app flask_app.app:create_app run

What it shows:

- one sync ``esf.Salesforce`` per process, shared by all threads, closed at exit;
- every request wrapped in ``esf.capture()`` (WSGI middleware), the roll-up logged on
  ``request.completed``;
- queries built with ``esf.soql`` and paginated with opaque cursors;
- single-record reads/writes and a bulk update through sObject Collections;
- typed errors mapped to HTTP responses.

(0.2 will ship ``even_simpler_salesforce.contrib.flask`` that packages this wiring.)
"""

from __future__ import annotations

import atexit
import logging
import os
import re
from collections.abc import Callable, Iterable
from typing import Any

from flask import Flask, Response, current_app, jsonify, request

import even_simpler_salesforce as esf

log = logging.getLogger("examples.flask_app")

LEAD_FIELDS = ["Id", "FirstName", "LastName", "Company", "Email", "Status"]
WRITABLE = ("FirstName", "LastName", "Company", "Email", "Status")
ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")

Environ = dict[str, Any]
OnSummary = Callable[[Environ, dict[str, Any]], None]


def client_from_env() -> esf.Salesforce:
    """The process-wide client (client-credentials flow, configured from the environment)."""
    return esf.Salesforce(
        esf.ClientCredentials(
            domain=os.environ["SF_DOMAIN"],
            client_id=os.environ["SF_CLIENT_ID"],
            client_secret=os.environ["SF_CLIENT_SECRET"],
        ),
        # Cursors go out to HTTP clients and come back; signing makes tampered ones a 400.
        cursor_key=os.environ["SF_CURSOR_KEY"].encode(),
    )


def log_summary(environ: Environ, fields: dict[str, Any]) -> None:
    """Merge the Salesforce roll-up into the app's canonical request log line."""
    method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO")
    log.info(
        "request.completed %s %s sf_calls=%s sf_ms=%s",
        method,
        path,
        fields["sf_calls"],
        fields["sf_ms"],
        extra={"http_method": method, "http_path": path, **fields},
    )


class SalesforceSummaryMiddleware:
    """WSGI middleware: wraps each request in ``esf.capture()`` and reports the roll-up."""

    def __init__(self, wsgi_app: Callable[..., Iterable[bytes]], on_summary: OnSummary) -> None:
        self.wsgi_app = wsgi_app
        self.on_summary = on_summary

    def __call__(self, environ: Environ, start_response: Callable[..., Any]) -> Iterable[bytes]:
        with esf.capture() as summary:
            try:
                return self.wsgi_app(environ, start_response)
            finally:
                self.on_summary(environ, summary.as_fields())


def sf() -> esf.Salesforce:
    """The process-wide client."""
    return current_app.extensions["salesforce"]


def _clean(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != "attributes"}


def _bad_request(message: str, status: int = 400) -> tuple[Response, int]:
    return jsonify({"error_code": "BAD_REQUEST", "message": message}), status


def _check_id(lead_id: str) -> None:
    if not ID_RE.match(lead_id):
        raise _InvalidInput("invalid Salesforce id")


class _InvalidInput(ValueError):
    pass


def create_app(client: Any = None, on_summary: OnSummary = log_summary) -> Flask:
    """Build the app. Tests pass a fake or a mocked client."""
    app = Flask(__name__)
    if client is None:
        client = client_from_env()
        atexit.register(client.close)
    app.extensions["salesforce"] = client
    app.wsgi_app = SalesforceSummaryMiddleware(app.wsgi_app, on_summary)  # type: ignore[method-assign]

    @app.errorhandler(_InvalidInput)
    def invalid(exc: _InvalidInput) -> tuple[Response, int]:
        return _bad_request(str(exc), 422)

    @app.errorhandler(esf.NotFound)
    def not_found(exc: esf.NotFound) -> tuple[Response, int]:
        return jsonify({"error_code": exc.error_code, "message": "not found"}), 404

    @app.errorhandler(esf.RecordError)
    def record_error(exc: esf.RecordError) -> tuple[Response, int]:
        return jsonify(
            # Validation messages help the caller fix input, but can quote record values: only
            # return them to callers allowed to see that record.
            {"error_code": exc.error_code, "message": exc.message, "fields": list(exc.fields)}
        ), 422

    @app.errorhandler(esf.MalformedQuery)
    def bad_cursor(exc: esf.MalformedQuery) -> tuple[Response, int]:
        return jsonify({"error_code": exc.error_code, "message": "invalid cursor"}), 400

    @app.errorhandler(esf.LimitError)
    def limited(exc: esf.LimitError) -> tuple[Response, int, dict[str, str]]:
        body = jsonify({"error_code": exc.error_code, "message": "Salesforce is busy"})
        return body, 503, {"Retry-After": "30"}

    @app.get("/leads")
    def list_leads() -> Any:
        """One page of leads; pass ``next_cursor`` back as ``cursor`` for the next page."""
        try:
            page_size = int(request.args.get("page_size", "200"))
        except ValueError:
            return _bad_request("page_size must be an integer")
        if not 200 <= page_size <= 2000:
            return _bad_request("page_size must be between 200 and 2000")
        cursor = request.args.get("cursor")
        if cursor:
            page = sf().query_more(cursor, page_size=page_size)
        else:
            soql = esf.soql(
                "SELECT Id, FirstName, LastName, Company, Email, Status FROM Lead "
                "WHERE Status = {} ORDER BY CreatedDate DESC",
                request.args.get("status", "Open"),
            )
            page = sf().query(soql, page_size=page_size)
        return {
            "records": [_clean(r) for r in page.records],
            "total_size": page.total_size,
            "next_cursor": page.cursor,
        }

    @app.get("/leads/<lead_id>")
    def get_lead(lead_id: str) -> Any:
        _check_id(lead_id)
        return _clean(sf().sobject("Lead").get(lead_id, fields=LEAD_FIELDS))

    @app.post("/leads")
    def create_lead() -> Any:
        body = request.get_json(silent=True) or {}
        if not body.get("LastName") or not body.get("Company"):
            return _bad_request("LastName and Company are required", 422)
        data = {k: body[k] for k in WRITABLE if body.get(k) is not None}
        return {"id": sf().sobject("Lead").create(data)}, 201

    @app.patch("/leads/<lead_id>")
    def update_lead(lead_id: str) -> Any:
        _check_id(lead_id)
        body = request.get_json(silent=True) or {}
        changes = {k: body[k] for k in WRITABLE if k in body}
        if changes:
            sf().sobject("Lead").update(lead_id, changes)
        return "", 204

    @app.post("/leads/bulk-status")
    def bulk_status() -> Any:
        """Set ``Status`` on many leads in as few round trips as possible (chunks of 200)."""
        body = request.get_json(silent=True) or {}
        ids, status = body.get("ids") or [], body.get("status")
        if not ids or not status or not isinstance(ids, list):
            return _bad_request("ids (non-empty list) and status are required", 422)
        for i in ids:
            _check_id(str(i))
        results = sf().collections.update("Lead", [{"Id": i, "Status": status} for i in ids])
        return {
            "updated": [r.id for r in results.succeeded],
            "failed": [
                {
                    "id": lead_id,
                    "errors": [
                        {"error_code": e.error_code, "message": e.message, "fields": list(e.fields)}
                        for e in r.errors
                    ],
                }
                for lead_id, r in zip(ids, results, strict=True)
                if not r.ok
            ],
        }

    @app.get("/usage")
    def usage() -> Any:
        """Per-process API usage counters (no API call)."""
        snap = sf().usage.snapshot()
        return {
            "api_used": snap.api_used,
            "api_max": snap.api_max,
            "calls_by_operation": dict(snap.calls_by_operation),
        }

    return app
