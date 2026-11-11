"""``esf describe``, ``esf request`` and ``esf generate models`` (spec §9.18).

Salesforce responses are ``synthetic`` (``tests/fixtures/recorded/sobject/describe_lead_fields.json``),
written from the ``Field`` properties of *DescribeSObjectResult*:
https://developer.salesforce.com/docs/atlas.en-us.api.meta/api/sforce_api_calls_describesobjects_describesobjectresult.htm
"""

from __future__ import annotations

import io
import json
import shlex
import sys
import typing
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx

from even_simpler_salesforce.cli import EXIT_API_ERROR, EXIT_OK, EXIT_USAGE, _codegen, _models, main

from ..fixtures import load_fixture

INSTANCE = "https://acme.my.salesforce.com"
API = f"{INSTANCE}/services/data/v68.0"
TOKEN = "00D000000000001!AQ.SECRET-ACCESS-TOKEN"
LEAD = load_fixture("sobject/describe_lead_fields.json")  # synthetic


@pytest.fixture(autouse=True)
def static_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for n in ("DOMAIN", "CLIENT_ID", "CLIENT_SECRET", "USERNAME", "PRIVATE_KEY_FILE", "API_VERSION", "DEBUG"):
        monkeypatch.delenv(f"ESF_{n}", raising=False)
    monkeypatch.setenv("ESF_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("ESF_INSTANCE_URL", INSTANCE)


def _output(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    out, err = capsys.readouterr()
    assert "SECRET" not in out + err
    return out, err


# --- describe ---------------------------------------------------------------------------


@respx.mock
def test_describe_table(capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.get(f"{API}/sobjects/Lead/describe").respond(json=LEAD)
    assert main(["describe", "Lead"]) == EXIT_OK
    out, _ = _output(capsys)
    lines = out.splitlines()
    assert lines[0].split() == ["field", "type", "length", "required", "values"]
    by_name = {line.split()[0]: line for line in lines[2:]}
    assert by_name["LastName"].split()[:4] == ["LastName", "string", "80", "yes"]
    assert "yes" not in by_name["Email"]  # nillable
    assert "yes" not in by_name["Status"]  # defaulted on create
    assert "yes" not in by_name["Id"]  # not createable
    assert "(restricted) Open, Working" in by_name["Status"]
    assert "Retired" not in by_name["Status"]  # inactive values are hidden
    assert by_name["LeadSource"].endswith("Web")
    assert route.call_count == 1


@respx.mock
def test_describe_json(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{API}/sobjects/Lead/describe").respond(json=LEAD)
    assert main(["describe", "Lead", "--json"]) == EXIT_OK
    rows = json.loads(_output(capsys)[0])
    status = next(r for r in rows if r["name"] == "Status")
    assert status == {
        "name": "Status",
        "type": "picklist",
        "length": 40,
        "required": False,
        "restricted": True,
        "values": ["Open", "Working"],
    }
    assert next(r for r in rows if r["name"] == "NumberOfEmployees")["length"] is None


@respx.mock
def test_describe_odd_bodies_and_unknown_type(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{API}/sobjects/Odd/describe").respond(json={"name": "Odd", "fields": ["x", {"name": "A"}]})
    assert main(["describe", "Odd", "--json"]) == EXIT_OK
    assert [r["name"] for r in json.loads(_output(capsys)[0])] == ["A"]
    respx.get(f"{API}/sobjects/Nope/describe").respond(
        404, json=[{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
    )
    assert main(["describe", "Nope"]) == EXIT_API_ERROR
    err = _output(capsys)[1]
    assert err.startswith("esf: InvalidType:")
    assert "  docs: " in err


# --- request ----------------------------------------------------------------------------


@respx.mock
def test_request_get_with_params(capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.get(f"{API}/query").respond(json={"totalSize": 0, "done": True, "records": []})
    assert (
        main(["request", "get", "query", "--param", "q=SELECT Id FROM Lead", "--param", "x=a=b"]) == EXIT_OK
    )
    assert json.loads(_output(capsys)[0]) == {"totalSize": 0, "done": True, "records": []}
    request = route.calls.last.request
    assert request.method == "GET"
    assert request.url.params["q"] == "SELECT Id FROM Lead"
    assert request.url.params["x"] == "a=b"


@respx.mock
def test_request_json_body_inline_and_from_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    route = respx.post(f"{INSTANCE}/services/apexrest/orders").respond(json={"ok": True})
    assert main(["request", "POST", "/services/apexrest/orders", "--json", '{"id": 1}']) == EXIT_OK
    assert json.loads(route.calls.last.request.content) == {"id": 1}
    body = tmp_path / "body.json"
    body.write_text('{"id": 2}', encoding="utf-8")
    assert main(["request", "POST", "/services/apexrest/orders", "--json", f"@{body}"]) == EXIT_OK
    assert json.loads(route.calls.last.request.content) == {"id": 2}
    _output(capsys)


@respx.mock
def test_request_empty_text_and_binary(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.delete(f"{API}/sobjects/Lead/00Q000000000001AAA").respond(204)
    assert main(["request", "DELETE", "sobjects/Lead/00Q000000000001AAA"]) == EXIT_OK
    assert route.called
    assert _output(capsys)[0] == ""

    respx.get(f"{API}/text").respond(text="hello\x1b[2J", headers={"content-type": "text/plain"})
    assert main(["request", "GET", "text"]) == EXIT_OK
    assert _output(capsys)[0] == "hello\x1b[2J"  # a pipe gets the body unchanged

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    tty = Tty()
    monkeypatch.setattr(sys, "stdout", tty)
    assert main(["request", "GET", "text"]) == EXIT_OK
    assert tty.getvalue() == "hello\\x1b[2J\n"  # a terminal gets escape sequences escaped

    respx.get(f"{API}/blob").respond(content=b"\x89PNG", headers={"content-type": "image/png"})

    class Binary(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.buffer = io.BytesIO()

    binary = Binary()
    monkeypatch.setattr(sys, "stdout", binary)
    assert main(["request", "GET", "blob"]) == EXIT_OK
    assert binary.buffer.getvalue() == b"\x89PNG"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["request", "POST", "x", "--json", "{nope"], "--json is not valid JSON"),
        (["request", "POST", "x", "--json", "@/no/such/file.json"], "cannot read /no/such/file.json"),
        (["request", "GET", "x", "--param", "novalue"], "--param needs KEY=VALUE"),
        (["request", "GET", "x", "--json", "{}"], "can't carry a body"),
    ],
)
def test_request_usage_errors(capsys: pytest.CaptureFixture[str], argv: list[str], message: str) -> None:
    with respx.mock(assert_all_called=False):
        assert main(argv) == EXIT_USAGE
    assert message in _output(capsys)[1]


@respx.mock
def test_request_api_error(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{API}/limits").respond(403, json=load_fixture("errors/request_limit.json"))
    assert main(["request", "GET", "limits"]) == EXIT_API_ERROR
    err = _output(capsys)[1]
    assert err.startswith("esf: RequestLimitExceeded: REQUEST_LIMIT_EXCEEDED")
    assert "  hint: " in err


# --- generate models --------------------------------------------------------------------


@pytest.fixture
def load(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import generated source as a real module (type hints resolve through ``sys.modules``)."""
    counter = iter(range(1000))

    def _load(source: str) -> ModuleType:
        name = f"esf_generated_models_{next(counter)}"
        module = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        exec(compile(source, f"<{name}>", "exec"), module.__dict__)  # noqa: S102 - our own generated file
        return module

    return _load


def test_python_type_mapping() -> None:
    assert _models.python_type({"type": "boolean"}) == "bool"
    assert _models.python_type({"type": "currency"}) == "float"
    assert _models.python_type({"type": "address"}) == "dict[str, Any]"
    assert _models.python_type({"type": "datetime"}) == "str"
    assert _models.python_type({"type": "picklist", "picklistValues": [{"value": "A"}]}) == "str"
    restricted = {"type": "picklist", "restrictedPicklist": True}
    assert _models.python_type(
        {**restricted, "picklistValues": [{"value": "A'B"}, {"value": "C", "active": False}]}
    ) == ('Literal["A\'B"]')
    assert _models.python_type({**restricted, "picklistValues": []}) == "str"
    assert _models.python_type({**restricted, "picklistValues": "odd"}) == "str"


@respx.mock
def test_generate_typeddict_models(capsys: pytest.CaptureFixture[str], tmp_path: Path, load: Any) -> None:
    respx.get(f"{API}/sobjects/Lead/describe").respond(json=LEAD)
    respx.get(f"{API}/sobjects/Account/describe").respond(
        json={"name": "Account", "fields": [{"name": "Id", "type": "id"}, {"name": "Name", "type": "string"}]}
    )
    target = tmp_path / "sf_models.py"
    assert main(["generate", "models", "Lead", "Account", "--out", str(target)]) == EXIT_OK
    assert _output(capsys)[1] == f"wrote {target}\n"
    source = target.read_text(encoding="utf-8")
    header = source.splitlines()[:5]
    assert header[0].startswith("# Typed sObject models, generated by esf.")
    assert header[1] == f"#   org:         {INSTANCE} (-)"
    assert header[2] == "#   api version: 68.0"
    assert header[4] == f"#   command:     esf generate models Lead Account --out {shlex.quote(str(target))}"

    module = load(source)
    assert module.__all__ == ["Lead", "Account"]
    lead = module.Lead
    assert typing.is_typeddict(lead)
    assert lead.__total__ is False
    hints = typing.get_type_hints(lead)
    assert hints["LastName"] is str
    assert hints["Email"] == str | None
    assert hints["Status"] == typing.Literal["Open", "Working"]
    assert hints["LeadSource"] == str | None  # unrestricted picklist
    assert hints["NumberOfEmployees"] == int | None
    assert hints["HasOptedOutOfEmail"] is bool
    assert hints["Address"] == dict[str, Any] | None
    assert hints["Score__c"] == float | None
    assert set(typing.get_type_hints(module.Account)) == {"Id", "Name"}


@respx.mock
def test_generate_pydantic_models_to_stdout(capsys: pytest.CaptureFixture[str], load: Any) -> None:
    pytest.importorskip("pydantic")
    respx.get(f"{API}/sobjects/Lead/describe").respond(json=LEAD)
    assert main(["generate", "models", "Lead", "--pydantic"]) == EXIT_OK
    source = _output(capsys)[0]
    assert "esf generate models Lead --pydantic" in source
    module = load(source)
    row = module.Lead.model_validate(
        {"attributes": {"type": "Lead"}, "Id": "00Q000000000001AAA", "Status": "Open", "Score__c": 4.5}
    )
    assert (row.Id, row.Status, row.Score__c, row.Email) == ("00Q000000000001AAA", "Open", 4.5, None)
    with pytest.raises(ValueError, match="Status"):
        module.Lead.model_validate({"Status": "Retired"})  # not an active value


def test_render_skips_names_that_are_not_identifiers(load: Any) -> None:
    describe = {"name": "Odd__c", "fields": [{"name": "class", "type": "string"}, {"name": "Ok__c"}, "x"]}
    source = _models.render_models([describe], pydantic=False, header="# h\n")
    assert "# skipped 'class': not a Python identifier" in source
    assert set(typing.get_type_hints(load(source).Odd__c)) == {"Ok__c"}
    assert "Ok__c" in _models.render_models(
        [{**describe, "fields": "odd"}, describe], pydantic=False, header=""
    )
    with pytest.raises(ValueError, match="isn't a Python identifier"):
        _models.render_models([{"name": "bad name"}], pydantic=False, header="")


def test_file_header_escapes_control_characters() -> None:
    header = _codegen.file_header(
        "Typed sObject models",
        command=["generate", "models", "Lead\n# injected"],
        instance_url=INSTANCE,
        org_id="00D000000000001EAA",
        api_version="68.0",
        now=datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc),
    )
    lines = header.splitlines()
    assert len(lines) == 5
    assert all(line.startswith("#") for line in lines)
    assert lines[1] == f"#   org:         {INSTANCE} (00D000000000001EAA)"
    assert lines[3] == "#   generated:   2026-11-10T12:00:00Z"
    assert "\\n# injected" in lines[4]


@respx.mock
def test_generate_out_errors(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    respx.get(f"{API}/sobjects/Lead/describe").respond(json=LEAD)
    assert main(["generate", "models", "Lead", "--out", str(tmp_path / "missing" / "x.py")]) == EXIT_USAGE
    assert "cannot write" in _output(capsys)[1]
    respx.get(f"{API}/sobjects/Nope/describe").mock(
        return_value=httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": "nope"}])
    )
    assert main(["generate", "models", "Nope"]) == EXIT_API_ERROR
