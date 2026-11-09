from __future__ import annotations

import datetime as dt
import decimal
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

import even_simpler_salesforce as esf
from even_simpler_salesforce.core.soql import (
    SoqlIdentifier,
    SoqlLiteral,
    quote,
    redact_soql,
    soql,
    soql_hash,
    soql_identifier,
    soql_like,
)

# --- quote ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "null"),
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (-42, "-42"),
        (1.5, "1.5"),
        (decimal.Decimal("10.250"), "10.250"),
        (decimal.Decimal("1E+3"), "1000"),
        ("abc", "'abc'"),
        ("", "''"),
        ("O'Brien", "'O\\'Brien'"),
        ('say "hi"', "'say \\\"hi\\\"'"),
        ("a\\b", "'a\\\\b'"),
        ("l1\nl2\rl3\tl4\bl5\fl6", "'l1\\nl2\\rl3\\tl4\\bl5\\fl6'"),
        ("100%_x", "'100%_x'"),  # LIKE wildcards are only escaped by soql_like
        (dt.date(2024, 2, 29), "2024-02-29"),
        (dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc), "2024-01-02T03:04:05Z"),
        (
            dt.datetime(2024, 1, 2, 5, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=2))),
            "2024-01-02T03:04:05Z",
        ),
        (["a", "b"], "('a','b')"),
        (("x",), "('x')"),
        ([1, None, True], "(1,null,true)"),
        (frozenset({7}), "(7)"),
        ({3}, "(3)"),
        (SoqlLiteral("'raw'"), "'raw'"),
        (SoqlIdentifier("Name"), "Name"),
    ],
)
def test_quote_values(value: object, expected: str) -> None:
    assert quote(value) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), decimal.Decimal("NaN")])
def test_quote_rejects_non_finite(bad: object) -> None:
    with pytest.raises(ValueError, match="NaN"):
        quote(bad)


def test_quote_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive"):
        quote(dt.datetime(2024, 1, 1))


def test_quote_rejects_empty_and_nested_sequences_and_unknown_types() -> None:
    with pytest.raises(ValueError, match="empty"):
        quote([])
    with pytest.raises(TypeError, match="nested"):
        quote([[1]])
    with pytest.raises(TypeError, match="dict"):
        quote({"a": 1})
    with pytest.raises(TypeError, match="bytes"):
        quote(b"x")


# --- soql() ---------------------------------------------------------------------------------


def test_soql_positional_and_named() -> None:
    q = soql(
        "SELECT Id FROM Lead WHERE Email = {} AND Status IN {} AND IsConverted = {conv}",
        "a@b.c",
        ["Open", "Working"],
        conv=False,
    )
    assert q == (
        "SELECT Id FROM Lead WHERE Email = 'a@b.c' AND Status IN ('Open','Working') AND IsConverted = false"
    )


def test_soql_is_exported_and_braces_escape() -> None:
    assert esf.soql("SELECT Id FROM X WHERE Name = {} AND a = '{{b}}'", "n") == (
        "SELECT Id FROM X WHERE Name = 'n' AND a = '{b}'"
    )


def test_soql_like_spec() -> None:
    assert soql("SELECT Id FROM A WHERE Name LIKE {:like}", "50%_off") == (
        "SELECT Id FROM A WHERE Name LIKE '50\\%\\_off'"
    )
    with pytest.raises(TypeError, match="like"):
        soql("{:like}", 5)


def test_soql_rejects_other_specs_and_conversions() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        soql("{:>10}", "x")
    with pytest.raises(ValueError, match="conversions"):
        soql("{!r}", "x")


def test_soql_injection_attempt_is_quoted() -> None:
    evil = "x' OR Name != 'y"
    q = soql("SELECT Id FROM Lead WHERE Name = {}", evil)
    assert q == "SELECT Id FROM Lead WHERE Name = 'x\\' OR Name != \\'y'"


def test_soql_identifier_passthrough() -> None:
    q = soql("SELECT {} FROM Lead", soql_identifier("Owner.Name"))
    assert q == "SELECT Owner.Name FROM Lead"


# --- soql_like -----------------------------------------------------------------------------


def test_soql_like() -> None:
    assert soql_like("a%b_c") == "'a\\%b\\_c'"
    assert soql_like("it's", "%{}%") == "'%it\\'s%'"
    assert isinstance(soql_like("x"), SoqlLiteral)
    assert soql("WHERE Name LIKE {}", soql_like("x", "{}%")) == "WHERE Name LIKE 'x%'"
    with pytest.raises(ValueError, match="exactly one"):
        soql_like("x", "%")
    with pytest.raises(ValueError, match="exactly one"):
        soql_like("x", "{}{}")


# --- soql_identifier -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Id", "Name", "Custom__c", "ns__Field__c", "Account.Owner.Name", "Rel__r.Name", "A1_b"]
)
def test_identifier_valid(name: str) -> None:
    assert soql_identifier(name) == name
    assert isinstance(soql_identifier(name), SoqlIdentifier)


@pytest.mark.parametrize(
    "name", ["", "1abc", "_x", "Name; DELETE", "a..b", "a.", ".a", "Na me", "Name'", "a-b", "Ü"]
)
def test_identifier_invalid(name: str) -> None:
    with pytest.raises(ValueError):
        soql_identifier(name)


def test_identifier_non_string() -> None:
    with pytest.raises(ValueError):
        soql_identifier(None)  # type: ignore[arg-type]


# --- property tests -------------------------------------------------------------------------


def _unescape(lit: str) -> str:
    """Inverse of the SOQL string escaping, used to check round-trips."""
    assert lit.startswith("'")
    assert lit.endswith("'")
    body = lit[1:-1]
    table = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            nxt = body[i + 1]
            out.append(table.get(nxt, "\\" + nxt))
            i += 2
        else:
            assert c not in "'\n\r\t\b\f", f"unescaped {c!r} in literal"
            out.append(c)
            i += 1
    return "".join(out)


@given(st.text())
def test_property_string_roundtrip(value: str) -> None:
    lit = quote(value)
    assert _unescape(lit) == value


@given(st.text())
def test_property_quoted_string_is_one_literal(value: str) -> None:
    # The literal must be exactly one string token: the redaction regex matches it whole.
    lit = quote(value)
    m = re.fullmatch(r"'(?:\\.|[^'\\])*'", lit, flags=re.DOTALL)
    assert m is not None


@given(st.text())
def test_property_injection_cannot_escape(value: str) -> None:
    q = soql("SELECT Id FROM Lead WHERE Name = {} LIMIT 1", value)
    assert q.startswith("SELECT Id FROM Lead WHERE Name = '")
    assert q.endswith("' LIMIT 1")
    assert redact_soql(q) == "SELECT Id FROM Lead WHERE Name = ? LIMIT ?"


@given(st.text())
def test_property_like_escapes_wildcards(value: str) -> None:
    lit = soql_like(value)
    raw = _unescape(lit)
    # After unescaping the SOQL string layer, every % and _ must still be backslash-escaped.
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw) and raw[i + 1] in "%_":
            i += 2
            continue
        assert raw[i] not in "%_"
        i += 1


@given(st.lists(st.one_of(st.text(), st.integers(), st.booleans(), st.none()), min_size=1))
def test_property_sequences(values: list[object]) -> None:
    out = quote(values)
    assert out.startswith("(")
    assert out.endswith(")")
    assert redact_soql(f"X IN {out}").startswith("X IN (")


@given(st.integers())
def test_property_ints(n: int) -> None:
    assert int(quote(n)) == n


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_property_floats(x: float) -> None:
    assert float(quote(x)) == x


# --- redaction / hashing ---------------------------------------------------------------------


def test_redact_soql() -> None:
    q = "SELECT  Id,Name FROM Lead\n WHERE Email = 'a@b.c' AND Amount > 10.5 AND N = -3 LIMIT 200"
    assert redact_soql(q) == "SELECT Id,Name FROM Lead WHERE Email = ? AND Amount > ? AND N = ? LIMIT ?"
    q2 = "SELECT Id FROM A WHERE CreatedDate > 2024-01-02T03:04:05Z AND D = 2024-01-02 AND F__c = 1"
    assert redact_soql(q2) == "SELECT Id FROM A WHERE CreatedDate > ? AND D = ? AND F__c = ?"
    q3 = "SELECT Id FROM A WHERE Name = 'O\\'Brien' AND X = 'y'"
    assert redact_soql(q3) == "SELECT Id FROM A WHERE Name = ? AND X = ?"
    # identifiers with digits are untouched
    assert redact_soql("SELECT Field1__c, A2 FROM Obj3") == "SELECT Field1__c, A2 FROM Obj3"


def test_redact_removes_secret_literals() -> None:
    secret = "hunter2-secret@example.com"
    assert secret not in redact_soql(soql("SELECT Id FROM Lead WHERE Email = {}", secret))


def test_soql_hash_normalises() -> None:
    a = soql_hash("SELECT Id FROM Lead WHERE Email = 'a@b.c'")
    b = soql_hash("SELECT  Id FROM Lead\nWHERE Email = 'zzz'")
    c = soql_hash("SELECT Id FROM Contact WHERE Email = 'a@b.c'")
    assert a == b != c
    assert re.fullmatch(r"[0-9a-f]{12}", a)
