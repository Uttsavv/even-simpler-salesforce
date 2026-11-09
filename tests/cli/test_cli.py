"""The ``esf`` CLI, against respx-mocked Salesforce endpoints."""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from even_simpler_salesforce.cli import EXIT_API_ERROR, EXIT_OK, EXIT_USAGE, main

INSTANCE = "https://acme.my.salesforce.com"
API = f"{INSTANCE}/services/data/v68.0"
TOKEN = "00D000000000001!AQ.SECRET-ACCESS-TOKEN"
ENV_NAMES = (
    "ACCESS_TOKEN",
    "INSTANCE_URL",
    "DOMAIN",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "USERNAME",
    "PRIVATE_KEY_FILE",
    "API_VERSION",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for n in ENV_NAMES:
        monkeypatch.delenv(f"ESF_{n}", raising=False)
    monkeypatch.delenv("ESF_DEBUG", raising=False)


@pytest.fixture
def static_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESF_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("ESF_INSTANCE_URL", INSTANCE)


def _rows(start: int, n: int) -> list[dict[str, object]]:
    return [
        {
            "attributes": {"type": "Lead", "url": f"/services/data/v68.0/sobjects/Lead/00Q{i:015d}"},
            "Id": f"00Q{i:015d}",
            "Name": f"Lead {i}",
            "Owner": {"attributes": {"type": "User"}, "Name": "Ann"},
            "IsConverted": False,
        }
        for i in range(start, start + n)
    ]


def _assert_no_token(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    out, err = capsys.readouterr()
    assert TOKEN not in out
    assert TOKEN not in err
    assert "SECRET" not in out + err
    return out, err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == EXIT_OK
    out, _ = capsys.readouterr()
    assert out.startswith("esf ")
    assert "httpx" in out


def test_help_and_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == EXIT_OK
    assert "whoami" in capsys.readouterr().out
    assert main([]) == EXIT_USAGE
    assert main(["nope"]) == EXIT_USAGE
    assert "invalid choice" in capsys.readouterr().err
    assert main(["query", "SELECT Id FROM Lead", "--json", "--csv"]) == EXIT_USAGE


def test_generate_is_0_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", "actions"]) == EXIT_USAGE
    assert "not available until 0.2" in capsys.readouterr().err


def test_missing_login(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert main(["limits"]) == EXIT_USAGE
    assert "no Salesforce login configured" in capsys.readouterr().err
    monkeypatch.setenv("ESF_ACCESS_TOKEN", TOKEN)
    assert main(["limits"]) == EXIT_USAGE
    err = _assert_no_token(capsys)[1]
    assert "ESF_INSTANCE_URL is not set" in err
    monkeypatch.setenv("ESF_INSTANCE_URL", "http://insecure")
    assert main(["limits"]) == EXIT_USAGE
    assert "https://" in _assert_no_token(capsys)[1]


def test_partial_client_credentials(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESF_DOMAIN", INSTANCE)
    monkeypatch.setenv("ESF_CLIENT_ID", "cid")
    assert main(["whoami"]) == EXIT_USAGE
    assert "ESF_CLIENT_SECRET" in capsys.readouterr().err
    monkeypatch.setenv("ESF_CLIENT_SECRET", "s")
    monkeypatch.setenv("ESF_DOMAIN", "login")
    assert main(["whoami"]) == EXIT_USAGE
    assert "My Domain" in capsys.readouterr().err


def test_partial_jwt(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ESF_USERNAME", "ann@example.com")
    assert main(["whoami"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "ESF_DOMAIN" in err
    assert "ESF_PRIVATE_KEY_FILE" in err
    monkeypatch.setenv("ESF_DOMAIN", "login")
    monkeypatch.setenv("ESF_CLIENT_ID", "cid")
    monkeypatch.setenv("ESF_PRIVATE_KEY_FILE", str(tmp_path / "missing.pem"))
    assert main(["whoami"]) == EXIT_USAGE
    assert "cannot read ESF_PRIVATE_KEY_FILE" in capsys.readouterr().err


def test_bad_api_version(
    capsys: pytest.CaptureFixture[str], static_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESF_API_VERSION", "50.0")
    assert main(["limits"]) == EXIT_USAGE
    assert "minimum" in capsys.readouterr().err


@respx.mock
def test_whoami_static(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    route = respx.get(f"{INSTANCE}/services/oauth2/userinfo").respond(
        json={
            "name": "Ann Admin",
            "preferred_username": "ann@acme.com",
            "email": "ann@acme.com",
            "user_id": "005000000000001AAA",
            "organization_id": "00D000000000001EAA",
        }
    )
    assert main(["whoami"]) == EXIT_OK
    out, _ = _assert_no_token(capsys)
    assert "Ann Admin" in out
    assert "ann@acme.com" in out
    assert "00D000000000001EAA" in out
    assert INSTANCE in out
    assert "static" in out
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert main(["whoami", "--json"]) == EXIT_OK
    data = json.loads(_assert_no_token(capsys)[0])
    assert data["user_id"] == "005000000000001AAA"
    assert data["auth"] == "static"


@respx.mock
def test_whoami_client_credentials(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ESF_DOMAIN", INSTANCE)
    monkeypatch.setenv("ESF_CLIENT_ID", "cid")
    monkeypatch.setenv("ESF_CLIENT_SECRET", "SECRET-client-secret")
    token = respx.post(f"{INSTANCE}/services/oauth2/token").respond(
        json={
            "access_token": TOKEN,
            "instance_url": INSTANCE,
            "id": "https://login.salesforce.com/id/00D000000000001EAA/005000000000001AAA",
            "issued_at": "1700000000000",
        }
    )
    respx.get(f"{INSTANCE}/services/oauth2/userinfo").respond(json={"name": "Integration"})
    assert main(["whoami", "--json"]) == EXIT_OK
    data = json.loads(_assert_no_token(capsys)[0])
    assert data["auth"] == "client_credentials"
    assert data["org_id"] == "00D000000000001EAA"  # falls back to the token identity
    assert b"grant_type=client_credentials" in token.calls[0].request.content


@respx.mock
def test_whoami_jwt(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    key_file = tmp_path / "server.key"
    key_file.write_bytes(pem)
    monkeypatch.setenv("ESF_DOMAIN", INSTANCE)
    monkeypatch.setenv("ESF_CLIENT_ID", "cid")
    monkeypatch.setenv("ESF_USERNAME", "ann@acme.com")
    monkeypatch.setenv("ESF_PRIVATE_KEY_FILE", str(key_file))
    token = respx.post(f"{INSTANCE}/services/oauth2/token").respond(
        json={"access_token": TOKEN, "instance_url": INSTANCE}
    )
    respx.get(f"{INSTANCE}/services/oauth2/userinfo").respond(json={"name": "Ann"})
    assert main(["whoami"]) == EXIT_OK
    out, _ = _assert_no_token(capsys)
    assert "jwt_bearer" in out
    assert b"jwt-bearer" in token.calls[0].request.content


@respx.mock
def test_limits_table_and_json(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    respx.get(f"{API}/limits").respond(
        json={
            "DailyApiRequests": {"Max": 15000, "Remaining": 14000, "Ann": {"Max": 1, "Remaining": 1}},
            "DataStorageMB": {"Max": 1024, "Remaining": 1000},
            "Weird": {"Max": 0, "Remaining": 0},
        }
    )
    assert main(["limits"]) == EXIT_OK
    out, _ = _assert_no_token(capsys)
    lines = out.splitlines()
    assert lines[0].split() == ["limit", "used", "max", "remaining", "used", "%"]
    daily = next(line for line in lines if line.startswith("DailyApiRequests"))
    assert daily.split()[1:] == ["1000", "15000", "14000", "6.7%"]
    assert next(line for line in lines if line.startswith("Weird")).split()[-1] == "-"
    assert main(["limits", "--json"]) == EXIT_OK
    data = json.loads(_assert_no_token(capsys)[0])
    assert data["DataStorageMB"] == {"max": 1024, "remaining": 1000, "used": 24}


@respx.mock
def test_query_first_page_table(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    route = respx.get(f"{API}/query").respond(
        json={
            "totalSize": 3,
            "done": False,
            "nextRecordsUrl": "/services/data/v68.0/query/01g000000000001AAA-2",
            "records": _rows(0, 2),
        }
    )
    assert main(["query", "SELECT Id, Name, Owner.Name, IsConverted FROM Lead"]) == EXIT_OK
    out, err = _assert_no_token(capsys)
    lines = out.splitlines()
    assert lines[0].split() == ["Id", "Name", "Owner.Name", "IsConverted"]
    assert "Lead 0" in lines[2]
    assert "false" in lines[2]
    assert "2 of 3 rows (more available; use --all)" in err
    assert route.calls[0].request.url.params["q"].startswith("SELECT Id")


@respx.mock
def test_query_all_json(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    respx.get(f"{API}/query").respond(
        json={
            "totalSize": 3,
            "done": False,
            "nextRecordsUrl": "/services/data/v68.0/query/01g000000000001AAA-2",
            "records": _rows(0, 2),
        }
    )
    more = respx.get(f"{API}/query/01g000000000001AAA-2").respond(
        json={"totalSize": 3, "done": True, "records": _rows(2, 1)}
    )
    assert main(["query", "SELECT Id FROM Lead", "--all", "--json", "--page-size", "200"]) == EXIT_OK
    out, err = _assert_no_token(capsys)
    data = json.loads(out)
    assert [r["Name"] for r in data] == ["Lead 0", "Lead 1", "Lead 2"]
    assert "attributes" not in data[0]
    assert "attributes" not in data[0]["Owner"]
    assert err.strip() == "3 rows"
    assert more.called
    assert more.calls[0].request.headers["Sforce-Query-Options"] == "batchSize=200"


@respx.mock
def test_query_csv_and_include_deleted(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    rows = _rows(0, 1)
    rows[0]["Contacts"] = {"totalSize": 1, "done": True, "records": [{"attributes": {}, "LastName": "Doe"}]}
    route = respx.get(f"{API}/queryAll").respond(json={"totalSize": 1, "done": True, "records": rows})
    assert main(["query", "SELECT Id FROM Lead", "--csv", "--include-deleted"]) == EXIT_OK
    out, err = _assert_no_token(capsys)
    parsed = list(csv.DictReader(io.StringIO(out)))
    assert parsed[0]["Owner.Name"] == "Ann"
    assert parsed[0]["IsConverted"] == "false"
    assert json.loads(parsed[0]["Contacts"]) == [{"LastName": "Doe"}]
    assert err.strip() == "1 row"
    assert route.called


@respx.mock
def test_query_empty(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    respx.get(f"{API}/query").respond(json={"totalSize": 0, "done": True, "records": []})
    assert main(["query", "SELECT Id FROM Lead WHERE Name = 'nobody'"]) == EXIT_OK
    out, err = capsys.readouterr()
    assert out == ""
    assert err.strip() == "0 rows"


@respx.mock
def test_api_errors_exit_1(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    respx.get(f"{API}/query").respond(
        400, json=[{"errorCode": "INVALID_FIELD", "message": "No such column 'Nope' on entity 'Lead'"}]
    )
    assert main(["query", "SELECT Nope FROM Lead"]) == EXIT_API_ERROR
    err = _assert_no_token(capsys)[1]
    assert "InvalidField" in err
    assert "No such column" in err


@respx.mock
def test_expired_session_exit_1(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    respx.get(f"{API}/limits").respond(
        401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired or invalid"}]
    )
    assert main(["limits"]) == EXIT_API_ERROR
    assert "SessionExpired" in _assert_no_token(capsys)[1]


@respx.mock
def test_transport_errors_exit_1(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    respx.get(f"{API}/limits").mock(side_effect=httpx.ConnectError("refused"))
    assert main(["limits"]) == EXIT_API_ERROR
    assert "ConnectError" in _assert_no_token(capsys)[1]


def test_bad_page_size(capsys: pytest.CaptureFixture[str], static_env: None) -> None:
    assert main(["query", "SELECT Id FROM Lead", "--page-size", "5"]) == EXIT_USAGE
    assert "page_size" in capsys.readouterr().err


def test_keyboard_interrupt(
    capsys: pytest.CaptureFixture[str], static_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import even_simpler_salesforce.cli as cli

    def boom(*a: object, **k: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_limits", boom)
    assert main(["limits"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_module_entry_point() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "even_simpler_salesforce.cli", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0
    assert out.stdout.startswith("esf ")
