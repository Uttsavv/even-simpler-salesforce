from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.auth import merge_token, parse_token_response, resolve_domain

from ..conftest import ID_URL, INSTANCE, ORG_ID, USER_ID, make_token, token_body

# --- Token ---------------------------------------------------------------------------------------


def test_token_repr_redacts() -> None:
    t = make_token("00Dxx!AQ_super_secret_access_ABCD", "5Aep_secret_refresh_WXYZ")
    for text in (repr(t), str(t), f"{t}", repr([t])):
        assert "super_secret" not in text
        assert "secret_refresh" not in text
        assert "ABCD" in text
        assert "WXYZ" in text
    short = make_token("abc", "defgh")
    assert "abc" not in repr(short).replace("access_token='…'", "")
    assert "defgh" not in repr(short)
    assert "refresh_token=None" in repr(make_token("x" * 20))
    assert "expires_at=None" in repr(make_token())
    assert "2030" in repr(make_token(expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc)))


def test_token_replace_and_frozen() -> None:
    t = make_token()
    t2 = t.replace(version=5)
    assert t2.version == 5
    assert t.version == 0
    with pytest.raises(AttributeError):
        t.version = 3  # type: ignore[misc]


def test_parse_token_response() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t = parse_token_response(token_body("AT", "RT", issued_at_ms=1_700_000_000_000), now=now)
    assert t.access_token == "AT"
    assert t.refresh_token == "RT"
    assert t.instance_url == INSTANCE
    assert t.issued_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert (t.org_id, t.user_id, t.id_url) == (ORG_ID, USER_ID, ID_URL)
    assert t.scope == frozenset({"api", "refresh_token"})
    assert t.signature == "c2lnbmF0dXJl"
    assert t.expires_at is None
    assert t.version == 0


def test_parse_token_response_minimal_and_bad() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t = parse_token_response(
        {"access_token": "a", "instance_url": INSTANCE + "/", "issued_at": "nope", "id": "https://x/other"},
        now=now,
    )
    assert t.issued_at == now
    assert t.instance_url == INSTANCE
    assert t.org_id is None
    assert t.user_id is None
    assert t.scope == frozenset()
    assert parse_token_response({"access_token": "a", "instance_url": INSTANCE}).refresh_token is None
    with pytest.raises(esf.UnexpectedResponse):
        parse_token_response({"access_token": "a"})
    with pytest.raises(esf.UnexpectedResponse):
        parse_token_response({"access_token": 1, "instance_url": INSTANCE})


@pytest.mark.parametrize(
    "instance_url",
    ["http://attacker.example", "ftp://acme.my.salesforce.com", "acme.my.salesforce.com", "https://"],
)
def test_parse_token_response_rejects_unsafe_instance_url(instance_url: str) -> None:
    """The bearer token goes to ``instance_url``: it must be https (http only for loopback)."""
    with pytest.raises(esf.UnexpectedResponse, match="instance_url"):
        parse_token_response({"access_token": "a", "instance_url": instance_url})


def test_parse_token_response_allows_loopback_http() -> None:
    t = parse_token_response({"access_token": "a", "instance_url": "http://localhost:8080/"})
    assert t.instance_url == "http://localhost:8080"


def test_merge_token() -> None:
    prev = make_token("old", "RT-old").replace(version=7, scope=frozenset({"api"}))
    new = esf.Token(access_token="new", instance_url=INSTANCE, issued_at=datetime.now(timezone.utc))
    merged = merge_token(new, prev)
    assert merged.access_token == "new"
    assert merged.refresh_token == "RT-old"
    assert (merged.org_id, merged.user_id, merged.id_url) == (ORG_ID, USER_ID, ID_URL)
    assert merged.scope == frozenset({"api"})
    assert merged.version == 7
    rotated = merge_token(new.replace(refresh_token="RT-new"), prev)
    assert rotated.refresh_token == "RT-new"
    assert merge_token(new, None) is new


def test_merge_token_refuses_another_identity() -> None:
    prev = make_token("old", "RT-old")
    other_user = prev.replace(access_token="new", user_id="005xx0000000002AAA")
    with pytest.raises(esf.TokenIdentityChanged, match="another user "):
        merge_token(other_user, prev)
    with pytest.raises(esf.TokenIdentityChanged, match="another org and user "):
        merge_token(other_user.replace(org_id="00Dxx0000000002EAA"), prev)
    # A 15-character id is the prefix of its 18-character form; a missing id isn't compared.
    assert merge_token(prev.replace(user_id=USER_ID[:15]), prev).user_id == USER_ID[:15]
    assert merge_token(prev.replace(org_id=None, user_id=None), prev).org_id == ORG_ID


# --- domains -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("login", "https://login.salesforce.com"),
        ("test", "https://test.salesforce.com"),
        ("https://acme.my.salesforce.com", "https://acme.my.salesforce.com"),
        ("https://acme.my.salesforce.com/", "https://acme.my.salesforce.com"),
    ],
)
def test_resolve_domain(domain: str, expected: str) -> None:
    assert resolve_domain(domain) == expected


@pytest.mark.parametrize(
    "domain",
    ["http://acme.my.salesforce.com", "acme.my.salesforce.com", "https://", "https://a.com/path", "prod"],
)
def test_resolve_domain_rejects(domain: str) -> None:
    with pytest.raises(ValueError):
        resolve_domain(domain)


# --- ClientCredentials -----------------------------------------------------------------------------


@pytest.mark.parametrize("domain", ["login", "test"])
def test_client_credentials_rejects_login_test(domain: str) -> None:
    with pytest.raises(ValueError, match="My Domain"):
        esf.ClientCredentials(domain=domain, client_id="c", client_secret="s")


def test_client_credentials() -> None:
    a = esf.ClientCredentials(domain=INSTANCE, client_id="c", client_secret="s3cr3t")
    assert a.token_url == INSTANCE + "/services/oauth2/token"
    assert a.token_form(None) == {
        "grant_type": "client_credentials",
        "client_id": "c",
        "client_secret": "s3cr3t",
    }
    assert a.token_key.startswith("client_credentials:")
    assert (
        esf.ClientCredentials(domain=INSTANCE, client_id="c", client_secret="s", token_key="k").token_key
        == "k"
    )
    assert "s3cr3t" not in repr(a)
    assert len(a.client_id_hash) == 12
    assert a.static_token() is None
    assert a.uses_refresh_token is False
    with pytest.raises(ValueError):
        esf.ClientCredentials(domain="http://x", client_id="c", client_secret="s")


# --- WebServerFlow + PKCE ---------------------------------------------------------------------------


def _flow(**kw: object) -> esf.WebServerFlow:
    return esf.WebServerFlow(
        domain="login",
        client_id="cid",
        redirect_uri="https://app/cb",
        client_secret="sec",
        **{"token_key": "user:1", **kw},  # type: ignore[arg-type]
    )


def test_authorization_url_pkce_s256() -> None:
    start = _flow().authorization_url(state="st-1", prompt="consent", login_hint="u@x.com")
    assert isinstance(start, esf.AuthStart)
    u = urlparse(start.url)
    assert f"{u.scheme}://{u.netloc}{u.path}" == "https://login.salesforce.com/services/oauth2/authorize"
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == "cid"
    assert q["redirect_uri"] == "https://app/cb"
    assert q["state"] == "st-1" == start.state
    assert q["scope"] == "api refresh_token"
    assert q["prompt"] == "consent"
    assert q["login_hint"] == "u@x.com"
    assert q["code_challenge_method"] == "S256"
    verifier = start.code_verifier
    assert verifier is not None
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected
    assert "=" not in q["code_challenge"]


def test_authorization_url_random_state_no_pkce_custom_scopes() -> None:
    a = _flow().authorization_url()
    b = _flow().authorization_url()
    assert a.state != b.state
    assert a.code_verifier != b.code_verifier
    s = _flow(pkce=False).authorization_url(scopes=[])
    q = parse_qs(urlparse(s.url).query)
    assert s.code_verifier is None
    assert "code_challenge" not in q
    assert "scope" not in q


def test_web_server_forms() -> None:
    f = _flow()
    assert f.code_form("CODE", "VER") == {
        "grant_type": "authorization_code",
        "code": "CODE",
        "redirect_uri": "https://app/cb",
        "client_id": "cid",
        "client_secret": "sec",
        "code_verifier": "VER",
    }
    assert "code_verifier" not in f.code_form("CODE", None)
    assert f.token_form(make_token(refresh="RT")) == {
        "grant_type": "refresh_token",
        "refresh_token": "RT",
        "client_id": "cid",
        "client_secret": "sec",
    }
    with pytest.raises(esf.ReauthorizationRequired):
        f.token_form(None)
    with pytest.raises(esf.ReauthorizationRequired):
        f.token_form(make_token())
    public = esf.WebServerFlow(domain="login", client_id="cid", redirect_uri="https://app/cb", token_key="u")
    assert "client_secret" not in public.token_form(make_token(refresh="RT"))
    assert f.uses_refresh_token is True
    assert f.token_key == "user:1"
    assert _flow(token_key="user:2").token_key == "user:2"
    for bad in ("", None):
        with pytest.raises(ValueError, match="token_key"):
            esf.WebServerFlow(domain="login", client_id="c", redirect_uri="r", token_key=bad)  # type: ignore[arg-type]
    assert "sec" not in repr(f).replace("Sequence", "")
    with pytest.raises(ValueError):
        esf.WebServerFlow(domain="nope", client_id="c", redirect_uri="r", token_key="u")


# --- JwtBearer ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return priv, pub


def test_jwt_assertion_decodes(rsa_keys: tuple[bytes, bytes]) -> None:
    import jwt

    priv, pub = rsa_keys
    a = esf.JwtBearer(domain=INSTANCE, client_id="cid", username="u@x.com", private_key=priv)
    tok = a.assertion(now=1_700_000_000)
    claims = jwt.decode(
        tok, pub, algorithms=["RS256"], audience="https://login.salesforce.com", options={"verify_exp": False}
    )
    assert claims == {
        "iss": "cid",
        "sub": "u@x.com",
        "aud": "https://login.salesforce.com",
        "exp": 1_700_000_180,
    }
    assert jwt.get_unverified_header(tok)["alg"] == "RS256"
    form = a.token_form(None)
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    live = jwt.decode(form["assertion"], pub, algorithms=["RS256"], audience="https://login.salesforce.com")
    assert live["sub"] == "u@x.com"
    assert "PRIVATE" not in repr(a)
    assert a.token_key.startswith("jwt_bearer:")


PASSPHRASE = "correct horse battery staple"


def _encrypted_pem(fmt: str) -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_format = {
        "pkcs8": serialization.PrivateFormat.PKCS8,
        "traditional": serialization.PrivateFormat.TraditionalOpenSSL,
    }[fmt]
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        private_format,
        serialization.BestAvailableEncryption(PASSPHRASE.encode()),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return priv, pub


@pytest.mark.parametrize("fmt", ["pkcs8", "traditional"])
@pytest.mark.parametrize("as_bytes", [False, True])
def test_jwt_encrypted_key_with_passphrase(fmt: str, as_bytes: bool) -> None:
    import jwt

    priv, pub = _encrypted_pem(fmt)
    a = esf.JwtBearer(
        domain=INSTANCE,
        client_id="cid",
        username="u@x.com",
        private_key=priv if as_bytes else priv.decode(),
        private_key_passphrase=PASSPHRASE.encode() if as_bytes else PASSPHRASE,
    )
    tok = a.token_form(None)["assertion"]
    assert (
        jwt.decode(tok, pub, algorithms=["RS256"], audience="https://login.salesforce.com")["sub"]
        == "u@x.com"
    )
    for text in (repr(a), str(a)):
        assert PASSPHRASE not in text
        assert "PRIVATE" not in text
        assert "passphrase" not in text
        assert "signing_key" not in text


def test_jwt_encrypted_key_errors_never_show_secrets(rsa_keys: tuple[bytes, bytes]) -> None:
    priv, _ = _encrypted_pem("pkcs8")
    plain, _ = rsa_keys

    def make(key: bytes, passphrase: str | None) -> esf.JwtBearer:
        return esf.JwtBearer(
            domain=INSTANCE, client_id="cid", username="u", private_key=key, private_key_passphrase=passphrase
        )

    cases = [
        (priv, None, "pass private_key_passphrase"),
        (priv, "wrong passphrase", "wrong private_key_passphrase"),
        (plain, PASSPHRASE, "isn't an encrypted PEM key"),
        (b"not a key", PASSPHRASE, "not a PEM private key"),
    ]
    for key, passphrase, expected in cases:
        with pytest.raises(ValueError, match=expected) as ei:
            make(key, passphrase)
        assert ei.value.__cause__ is None
        assert ei.value.__context__ is None or ei.value.__suppress_context__
        text = str(ei.value) + repr(ei.value)
        assert "PRIVATE" not in text
        assert PASSPHRASE not in text
        if passphrase:
            assert passphrase not in text


@pytest.mark.parametrize(
    ("domain", "audience", "aud"),
    [
        ("login", None, "https://login.salesforce.com"),
        ("test", None, "https://test.salesforce.com"),
        ("https://acme--dev.sandbox.my.salesforce.com", None, "https://test.salesforce.com"),
        ("https://test.salesforce.com", None, "https://test.salesforce.com"),
        ("https://TEST.salesforce.com/", None, "https://test.salesforce.com"),
        ("https://login.salesforce.com", None, "https://login.salesforce.com"),
        ("https://test.salesforce.com.attacker.example", None, "https://login.salesforce.com"),
        (INSTANCE, "test", "https://test.salesforce.com"),
        (INSTANCE, "login", "https://login.salesforce.com"),
        (INSTANCE, "https://custom.example.com", "https://custom.example.com"),
    ],
)
def test_jwt_audience(domain: str, audience: str | None, aud: str) -> None:
    a = esf.JwtBearer(domain=domain, client_id="c", username="u", private_key="k", audience=audience)
    assert a.aud == aud


# --- StaticToken ---------------------------------------------------------------------------------------


def test_static_token() -> None:
    s = esf.StaticToken("00D!secret-token-value", INSTANCE + "/")
    assert s.instance_url == INSTANCE
    assert "secret-token" not in repr(s)
    tok = s.static_token()
    assert tok is not None
    assert tok.access_token == "00D!secret-token-value"
    assert s.client_id_hash == ""
    assert s.token_key.startswith("static:")
    assert s.refresh_ahead is None
    assert s.expiry == "unknown"


def test_flows_accept_refresh_ahead_and_expiry() -> None:
    f = _flow(refresh_ahead=timedelta(minutes=30), expiry="introspect")
    assert f.refresh_ahead == timedelta(minutes=30)
    assert f.expiry == "introspect"
    c = esf.ClientCredentials(domain=INSTANCE, client_id="c", client_secret="s", expiry="introspect")
    assert c.expiry == "introspect"


def test_jwt_audience_uses_hostname_not_substring() -> None:
    """CodeQL py/incomplete-url-substring-sanitization: a look-alike host is not a sandbox."""
    pytest.importorskip("jwt")

    def aud(domain: str) -> str:
        return esf.JwtBearer(domain=domain, client_id="c", username="u", private_key="k").aud

    assert aud("https://acme--dev.sandbox.my.salesforce.com") == "https://test.salesforce.com"
    assert aud("https://x.sandbox.my.salesforce.com.attacker.example") == "https://login.salesforce.com"


def test_auth_start_repr_hides_pkce_verifier_and_state() -> None:
    start = _flow().authorization_url(state="st-secret-1")
    assert start.code_verifier
    text = repr(start)
    assert start.code_verifier not in text
    # state is part of the URL anyway; it is not repeated as a field.
    assert ", state=" not in text
    assert "code_verifier" not in text
    assert start.url.split("?")[0] in text
