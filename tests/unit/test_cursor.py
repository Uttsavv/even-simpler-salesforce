from __future__ import annotations

import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.cursor import Cursor, decode_cursor, encode_cursor

LOC = "/services/data/v68.0/query/01gxx0000000001AAA-2000"
LOC_ALL = "/services/data/v59.0/queryAll/01gxx00000000Ab-200"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).rstrip(b"=").decode()


@pytest.mark.parametrize("path", [LOC, LOC_ALL])
def test_roundtrip_unsigned(path: str) -> None:
    c = encode_cursor(path)
    assert isinstance(c, Cursor)
    assert isinstance(c, esf.Cursor)
    assert isinstance(c, str)
    assert "/" not in c
    assert "=" not in c
    assert "." not in c
    assert decode_cursor(c) == path


def test_roundtrip_signed() -> None:
    c = encode_cursor(LOC, key=b"k1")
    assert "." in c
    assert decode_cursor(c, key=b"k1") == LOC


def test_signed_rejected_with_other_key_or_without_key() -> None:
    c = encode_cursor(LOC, key=b"k1")
    with pytest.raises(esf.MalformedQuery, match="signature"):
        decode_cursor(c, key=b"k2")
    with pytest.raises(esf.MalformedQuery, match="no cursor_key"):
        decode_cursor(c)


def test_unsigned_rejected_when_key_configured() -> None:
    with pytest.raises(esf.MalformedQuery, match="signature") as ei:
        decode_cursor(encode_cursor(LOC), key=b"k1")
    assert ei.value.error_code == "INVALID_CURSOR"


def test_tampered_payload_rejected() -> None:
    c = encode_cursor(LOC, key=b"k1")
    payload, tag = c.split(".")
    other = _b64("/services/data/v68.0/query/01gxx0000000002AAA-4000")
    with pytest.raises(esf.MalformedQuery):
        decode_cursor(f"{other}.{tag}", key=b"k1")
    flipped = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(esf.MalformedQuery):
        decode_cursor(f"{flipped}.{tag}", key=b"k1")


@pytest.mark.parametrize(
    "path",
    [
        "/services/data/v68.0/sobjects/Account",
        "https://evil.example.com/services/data/v68.0/query/01gxx0000000001AAA-2000",
        "/services/data/v68.0/query/01gxx0000000001AAA-2000/../../sobjects",
        "/services/data/v68.0/query/short-1",
        "/services/data/v68/query/01gxx0000000001AAA-2000",
        "/services/apexrest/query/01gxx0000000001AAA-2000",
        "/services/data/v68.0/query/01gxx0000000001AAA-2000?x=1",
        "",
    ],
)
def test_foreign_shapes_rejected(path: str) -> None:
    with pytest.raises(esf.MalformedQuery):
        encode_cursor(path)
    if path:
        with pytest.raises(esf.MalformedQuery):
            decode_cursor(_b64(path))


@pytest.mark.parametrize("bad", ["", "!!!!", "a" * 513, "%%%", _b64("\xff\xfe") + "x"])
def test_garbage_rejected(bad: str) -> None:
    with pytest.raises(esf.MalformedQuery):
        decode_cursor(bad)


def test_non_ascii_payload_rejected() -> None:
    raw = base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode().rstrip("=")
    with pytest.raises(esf.MalformedQuery):
        decode_cursor(raw)


def test_non_string_rejected() -> None:
    with pytest.raises(esf.MalformedQuery):
        decode_cursor(None)  # type: ignore[arg-type]


@given(st.text(max_size=600))
def test_property_decode_never_crashes(text: str) -> None:
    try:
        path = decode_cursor(text)
    except esf.MalformedQuery:
        return
    assert path.startswith("/services/data/v")


@given(
    st.from_regex(r"[0-9A-Za-z]{15,18}", fullmatch=True),
    st.integers(min_value=0, max_value=10**9),
    st.binary(min_size=1, max_size=32),
)
def test_property_signed_roundtrip(locator: str, offset: int, key: bytes) -> None:
    path = f"/services/data/v68.0/query/{locator}-{offset}"
    assert decode_cursor(encode_cursor(path, key), key) == path
