"""Sample Flask app using only even-simpler-salesforce's public API.

Run it (from the ``examples/`` directory)::

    export SF_DOMAIN=https://acme.my.salesforce.com SF_CLIENT_ID=... SF_CLIENT_SECRET=...
    # Signs pagination cursors. Generate it once, keep it secret, share it across workers:
    export SF_CURSOR_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    flask --app flask_app.app:create_app run

What it shows:

- ``even_simpler_salesforce.contrib.flask``: one sync ``esf.Salesforce`` per process, shared by
  all threads, closed at exit;
- every request's Salesforce roll-up logged on ``request.completed``;
- queries built with ``esf.soql`` and paginated with opaque cursors;
- single-record reads/writes and a bulk update through sObject Collections;
- typed errors mapped to HTTP responses by the extension.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from typing import Any

from flask import Flask, Request, Response, jsonify, request

import even_simpler_salesforce as esf
from even_simpler_salesforce.contrib.flask import Salesforce

log = logging.getLogger("examples.flask_app")

LEAD_FIELDS = ["Id", "FirstName", "LastName", "Company", "Email", "Status"]
WRITABLE = ("FirstName", "LastName", "Company", "Email", "Status")
ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")

OnSummary = Callable[[Request, dict[str, Any]], None]


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


def log_summary(request: Request, fields: dict[str, Any]) -> None:
    """Merge the Salesforce roll-up into the app's canonical request log line."""
    log.info(
        "request.completed %s %s sf_calls=%s sf_ms=%s",
        request.method,
        request.path,
        fields["sf_calls"],
        fields["sf_ms"],
        extra={"http_method": request.method, "http_path": request.path, **fields},
    )


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
    ext = Salesforce(
        app,
        client=client if client is not None else client_from_env(),  # closed at process exit
        on_summary=on_summary,
        error_handlers=True,  # NotFound -> 404, RecordError -> 422, bad cursor -> 400, limits -> 503, ...
    )

    def sf() -> esf.Salesforce:
        return ext.client

    @app.errorhandler(_InvalidInput)
    def invalid(exc: _InvalidInput) -> tuple[Response, int]:
        return _bad_request(str(exc), 422)

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
