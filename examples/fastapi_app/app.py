"""Sample FastAPI app using only even-simpler-salesforce's public API.

Run it (from the ``examples/`` directory)::

    export SF_DOMAIN=https://acme.my.salesforce.com SF_CLIENT_ID=... SF_CLIENT_SECRET=...
    # Signs pagination cursors. Generate it once, keep it secret, share it across workers:
    export SF_CURSOR_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    uvicorn fastapi_app.app:app --reload

What it shows:

- ``even_simpler_salesforce.contrib.fastapi``: one ``AsyncSalesforce`` per process, closed in
  the app lifespan, handed to routes with a dependency;
- every HTTP request's Salesforce roll-up logged on ``request.completed``;
- queries built with ``esf.soql`` and paginated with opaque cursors;
- single-record reads/writes and a bulk update through sObject Collections;
- typed errors mapped to HTTP responses by the integration.
"""

# No ``from __future__ import annotations``: FastAPI resolves the route annotations at runtime,
# and ``SF`` is local to ``create_app()``.

import logging
import os
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Path, Query, Request, Response
from pydantic import BaseModel, Field

import even_simpler_salesforce as esf
from even_simpler_salesforce.contrib.fastapi import SalesforceIntegration

log = logging.getLogger("examples.fastapi_app")

LEAD_FIELDS = ["Id", "FirstName", "LastName", "Company", "Email", "Status"]
ID_PATTERN = r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$"

OnSummary = Callable[[Request, dict[str, Any]], None]


# --- wiring -------------------------------------------------------------------------------


def client_from_env() -> esf.AsyncSalesforce:
    """The app-wide client (client-credentials flow, configured from the environment)."""
    return esf.AsyncSalesforce(
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
        request.url.path,
        fields["sf_calls"],
        fields["sf_ms"],
        extra={"http_method": request.method, "http_path": request.url.path, **fields},
    )


LeadId = Annotated[str, Path(pattern=ID_PATTERN)]


# --- request/response models ------------------------------------------------------------


class LeadIn(BaseModel):
    LastName: str = Field(min_length=1)
    Company: str = Field(min_length=1)
    FirstName: str | None = None
    Email: str | None = None
    Status: str | None = None


class LeadPatch(BaseModel):
    FirstName: str | None = None
    LastName: str | None = None
    Company: str | None = None
    Email: str | None = None
    Status: str | None = None


class BulkStatus(BaseModel):
    ids: list[Annotated[str, Field(pattern=ID_PATTERN)]] = Field(min_length=1, max_length=2000)
    status: str = Field(min_length=1)


def _clean(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != "attributes"}


# --- app ----------------------------------------------------------------------------------


def create_app(client: Any = None, on_summary: OnSummary = log_summary) -> FastAPI:
    """Build the app. Tests pass a fake or a mocked client."""
    esf_app = SalesforceIntegration(
        client=client if client is not None else client_from_env(),
        on_summary=on_summary,
        error_handlers=True,  # NotFound -> 404, RecordError -> 422, bad cursor -> 400, limits -> 503, ...
    )
    app = FastAPI(title="Leads (even-simpler-salesforce example)", lifespan=esf_app.lifespan)
    esf_app.init_app(app)
    SF = Annotated[esf.AsyncSalesforce, Depends(esf_app.admin)]

    @app.get("/leads")
    async def list_leads(
        sf: SF,
        status: str = "Open",
        cursor: str | None = None,
        page_size: Annotated[int, Query(ge=200, le=2000)] = 200,
    ) -> dict[str, Any]:
        """One page of leads; pass ``next_cursor`` back as ``cursor`` for the next page."""
        if cursor:
            page = await sf.query_more(cursor, page_size=page_size)
        else:
            soql = esf.soql(
                "SELECT Id, FirstName, LastName, Company, Email, Status FROM Lead "
                "WHERE Status = {} ORDER BY CreatedDate DESC",
                status,
            )
            page = await sf.query(soql, page_size=page_size)
        return {
            "records": [_clean(r) for r in page.records],
            "total_size": page.total_size,
            "next_cursor": page.cursor,
        }

    @app.get("/leads/{lead_id}")
    async def get_lead(lead_id: LeadId, sf: SF) -> dict[str, Any]:
        return _clean(await sf.sobject("Lead").get(lead_id, fields=LEAD_FIELDS))

    @app.post("/leads", status_code=201)
    async def create_lead(body: LeadIn, sf: SF) -> dict[str, str]:
        lead_id = await sf.sobject("Lead").create(body.model_dump(exclude_none=True))
        return {"id": lead_id}

    @app.patch("/leads/{lead_id}", status_code=204)
    async def update_lead(lead_id: LeadId, body: LeadPatch, sf: SF) -> Response:
        changes = body.model_dump(exclude_unset=True)
        if changes:
            await sf.sobject("Lead").update(lead_id, changes)
        return Response(status_code=204)

    @app.post("/leads/bulk-status")
    async def bulk_status(body: BulkStatus, sf: SF) -> dict[str, Any]:
        """Set ``Status`` on many leads in as few round trips as possible (chunks of 200)."""
        results = await sf.collections.update("Lead", [{"Id": i, "Status": body.status} for i in body.ids])
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
                for lead_id, r in zip(body.ids, results, strict=True)
                if not r.ok
            ],
        }

    @app.get("/usage")
    async def usage(sf: SF) -> dict[str, Any]:
        """Per-process API usage counters (no API call)."""
        snap = sf.usage.snapshot()
        return {
            "api_used": snap.api_used,
            "api_max": snap.api_max,
            "calls_by_operation": dict(snap.calls_by_operation),
        }

    return app


def __getattr__(name: str) -> Any:
    # ``uvicorn fastapi_app.app:app`` builds the app lazily so importing this module in tests
    # doesn't need SF_* variables.
    if name == "app":
        return create_app()
    raise AttributeError(name)
