"""``even_simpler_salesforce.contrib.flask`` driven through Flask's own test client.

Framework behaviour these tests rely on:

- Extensions follow the ``init_app`` pattern, keep per-app state in ``app.extensions`` and read
  it through ``current_app`` (https://flask.palletsprojects.com/en/stable/extensiondev/).
- ``before_request`` / ``teardown_request`` bracket each request, and teardown runs even when
  the view raised (https://flask.palletsprojects.com/en/stable/reqcontext/).
- Error handlers are looked up along the exception's MRO, so an app's handler for a subclass
  wins (https://flask.palletsprojects.com/en/stable/errorhandling/).
"""

from __future__ import annotations

import atexit
from typing import Any

import httpx
import pytest
import respx
from flask import Flask, Request

import even_simpler_salesforce as esf
from even_simpler_salesforce.contrib.flask import EXTENSION, Salesforce
from even_simpler_salesforce.testing import FakeSalesforce

INSTANCE = "https://acme.my.salesforce.com"


def build(ext: Salesforce, *, init: bool = True) -> Flask:
    app = Flask(__name__)
    if init:
        ext.init_app(app)

    @app.get("/leads/<lead_id>")
    def get_lead(lead_id: str) -> dict[str, Any]:
        return ext.client.sobject("Lead").get(lead_id, fields=["Id", "LastName"])

    @app.get("/count")
    def count() -> dict[str, int]:
        return {"n": ext.client.count("SELECT COUNT() FROM Lead")}

    @app.get("/plain")
    def plain() -> str:
        return "ok"

    return app


@pytest.fixture
def summaries() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture
def app(esf_fake: FakeSalesforce, summaries: list[tuple[str, dict[str, Any]]]) -> Flask:
    def on_summary(request: Request, fields: dict[str, Any]) -> None:
        summaries.append((request.path, fields))

    return build(Salesforce(client=esf_fake, on_summary=on_summary, error_handlers=True))


def test_needs_exactly_one_of_auth_or_client(esf_fake: FakeSalesforce) -> None:
    with pytest.raises(TypeError, match="exactly one"):
        Salesforce()
    with pytest.raises(TypeError, match="exactly one"):
        Salesforce(auth=esf.StaticToken("t", INSTANCE), client=esf_fake)
    with pytest.raises(TypeError, match="store="):
        Salesforce(client=esf_fake, store=esf.InMemoryTokenStore())


def test_client_and_summary(
    app: Flask, esf_fake: FakeSalesforce, summaries: list[tuple[str, dict[str, Any]]]
) -> None:
    lead_id = esf_fake.add("Lead", {"LastName": "Doe", "Company": "Acme"})
    resp = app.test_client().get(f"/leads/{lead_id}")
    assert resp.status_code == 200
    assert resp.json is not None
    assert resp.json["LastName"] == "Doe"
    path, fields = summaries[-1]
    assert path == f"/leads/{lead_id}"
    assert (fields["sf_calls"], fields["sf_ops"]) == (1, {"sobject.get": 1})
    app.test_client().get("/plain")
    assert summaries[-1] == ("/plain", {**fields, "sf_calls": 0, "sf_ms": 0, "sf_ops": {}})


def test_app_passed_to_constructor(esf_fake: FakeSalesforce) -> None:
    app = Flask(__name__)
    ext = Salesforce(app, client=esf_fake)
    assert app.extensions[EXTENSION] is esf_fake
    with app.app_context():
        assert ext.client is esf_fake  # type: ignore[comparison-overlap]


def test_auth_builds_one_client_per_app() -> None:
    store = esf.InMemoryTokenStore()
    ext = Salesforce(auth=esf.StaticToken("t", INSTANCE), store=store)
    one, two = build(ext), build(ext)
    with one.app_context():
        first = ext.client
    with two.app_context():
        second = ext.client
    assert isinstance(first, esf.Salesforce)
    assert first is not second
    assert first._store is store


def test_client_outside_init_app_or_context(esf_fake: FakeSalesforce) -> None:
    ext = Salesforce(client=esf_fake)
    app = build(ext, init=False)
    with app.app_context(), pytest.raises(RuntimeError, match="init_app"):
        _ = ext.client
    with pytest.raises(RuntimeError):  # Flask: working outside of application context
        _ = ext.client


def test_client_closed_at_exit(monkeypatch: pytest.MonkeyPatch, esf_fake: FakeSalesforce) -> None:
    registered: list[Any] = []
    monkeypatch.setattr(atexit, "register", registered.append)
    Salesforce(Flask(__name__), client=esf_fake)
    assert len(registered) == 1
    registered[0]()
    assert esf_fake.closed


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (esf.ReauthorizationRequired("revoked"), 401, "salesforce_reauth_required"),
        (
            esf.RequiredFieldMissing("need it", error_code="REQUIRED_FIELD_MISSING"),
            422,
            "salesforce_record_error",
        ),
        (esf.NotFound("gone", error_code="NOT_FOUND"), 404, "salesforce_not_found"),
        (esf.MalformedQuery("invalid cursor", error_code="INVALID_CURSOR"), 400, "salesforce_invalid_cursor"),
        (esf.BudgetExceeded("budget"), 503, "salesforce_limit"),
        (esf.ConnectError("refused"), 502, "salesforce_unavailable"),
        (esf.ServerError("down"), 503, "salesforce_unavailable"),
    ],
)
def test_error_handlers(
    app: Flask, esf_fake: FakeSalesforce, error: Exception, status: int, code: str
) -> None:
    esf_fake.fail_next("sobject.get", error)
    resp = app.test_client().get("/leads/00Q000000000001AAA")
    assert resp.status_code == status
    assert resp.json is not None
    assert resp.json["code"] == code
    if code == "salesforce_limit":
        assert resp.headers["Retry-After"] == "30"


def test_unmapped_errors_stay_500(
    app: Flask, esf_fake: FakeSalesforce, summaries: list[tuple[str, dict[str, Any]]]
) -> None:
    esf_fake.fail_next("query", esf.MalformedQuery("unexpected token", error_code="MALFORMED_QUERY"))
    assert app.test_client().get("/count").status_code == 500
    assert summaries[-1][1]["sf_errors"] == ["MALFORMED_QUERY"]  # reported even when the view raised


def test_handlers_off_by_default(esf_fake: FakeSalesforce) -> None:
    app = build(Salesforce(client=esf_fake))
    esf_fake.fail_next("sobject.get", esf.NotFound("gone"))
    assert app.test_client().get("/leads/00Q000000000001AAA").status_code == 500


def test_app_handler_for_a_subclass_wins(app: Flask, esf_fake: FakeSalesforce) -> None:
    @app.errorhandler(esf.RowLocked)
    def locked(exc: esf.RowLocked) -> tuple[dict[str, str], int]:
        return {"code": "busy"}, 409

    esf_fake.fail_next("sobject.get", esf.RowLocked("locked", error_code="UNABLE_TO_LOCK_ROW"))
    resp = app.test_client().get("/leads/00Q000000000001AAA")
    assert (resp.status_code, resp.json) == (409, {"code": "busy"})


def test_no_summary_when_an_earlier_hook_ends_the_request(
    esf_fake: FakeSalesforce, summaries: list[tuple[str, dict[str, Any]]]
) -> None:
    app = Flask(__name__)
    app.before_request(lambda: ("blocked", 403))
    Salesforce(
        app, client=esf_fake, on_summary=lambda request, fields: summaries.append((request.path, fields))
    )
    assert app.test_client().get("/anything").status_code == 403
    assert summaries == []


def test_app_context_without_request_reports_nothing(
    app: Flask, summaries: list[tuple[str, dict[str, Any]]]
) -> None:
    with app.app_context():
        pass
    assert summaries == []


@respx.mock
def test_real_client_summary_fields(summaries: list[tuple[str, dict[str, Any]]]) -> None:
    route = respx.get(url__regex=rf"^{INSTANCE}/services/data/v[\d.]+/query").mock(
        return_value=httpx.Response(
            200,
            json={"totalSize": 3, "done": True, "records": []},
            headers={"Sforce-Limit-Info": "api-usage=1200/15000"},
        )
    )
    ext = Salesforce(
        auth=esf.StaticToken("app-token", INSTANCE),
        on_summary=lambda request, fields: summaries.append((request.method, fields)),
    )
    app = build(ext)
    assert app.test_client().get("/count").json == {"n": 3}
    assert route.calls[0].request.headers["Authorization"] == "Bearer app-token"
    method, fields = summaries[-1]
    assert method == "GET"
    assert (fields["sf_calls"], fields["sf_api_used"], fields["sf_api_max"]) == (1, 1200, 15000)
