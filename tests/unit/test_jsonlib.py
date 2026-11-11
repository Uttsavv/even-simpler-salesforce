"""Both JSON backends behave identically (simple-salesforce #442, #309: NaN became null)."""

from __future__ import annotations

import datetime as dt
import decimal
import json
from typing import Any

import pytest

from even_simpler_salesforce.core import jsonlib

BACKENDS = [jsonlib.std_dumps, jsonlib.orjson_dumps]
D = decimal.Decimal


@pytest.mark.parametrize("dumps", BACKENDS)
@pytest.mark.parametrize(
    "bad",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        D("NaN"),
        D("sNaN"),
        D("Infinity"),
        {"a": float("nan")},
        {"a": {"b": [1, {"c": float("inf")}]}},
        [D("-Infinity")],
        (1.0, float("nan")),
        {float("nan")},
        frozenset({float("inf")}),
    ],
)
def test_non_finite_rejected_before_sending(dumps: Any, bad: Any) -> None:
    with pytest.raises(jsonlib.NonFiniteNumber):
        dumps(bad)
    assert issubclass(jsonlib.NonFiniteNumber, ValueError)


@pytest.mark.parametrize("dumps", BACKENDS)
def test_error_names_the_path(dumps: Any) -> None:
    with pytest.raises(ValueError, match=r"\$\.a\.b\[1\]"):
        dumps({"a": {"b": [1.0, float("nan")]}})


@pytest.mark.parametrize("dumps", BACKENDS)
@pytest.mark.parametrize("value", ["12.3400", "1E+2", "-0", "0.000000001", "123456789012345678901234.5"])
def test_decimal_is_an_exact_json_number(dumps: Any, value: str) -> None:
    out = dumps({"amount": D(value), "list": [D(value)]})
    parsed = json.loads(out, parse_float=D, parse_int=D)
    assert parsed["amount"] == D(value)
    assert parsed["list"] == [D(value)]
    assert b'"' + value.encode() + b'"' not in out  # never a string


@pytest.mark.parametrize("dumps", BACKENDS)
def test_backends_agree(dumps: Any) -> None:
    obj = {"s": "x\x00esf-dec-00:1", "n": 1.5, "i": 3, "b": True, "z": None, "u": "ü", "d": D("2.50")}
    assert json.loads(dumps(obj), parse_float=D) == json.loads(jsonlib.std_dumps(obj), parse_float=D)
    assert json.loads(dumps(obj))["s"] == "x\x00esf-dec-00:1"  # look-alike strings untouched


def test_loads_both_backends() -> None:
    assert jsonlib.std_loads(b'{"a":1}') == {"a": 1}
    assert jsonlib.orjson_loads(b'{"a":1}') == {"a": 1}
    assert jsonlib.BACKEND in ("orjson", "json")


@pytest.mark.parametrize("dumps", BACKENDS)
def test_unsupported_type(dumps: Any) -> None:
    with pytest.raises(TypeError):
        dumps({"x": object()})


@pytest.mark.parametrize("dumps", BACKENDS)
def test_dates_and_times_use_isoformat(dumps: Any) -> None:
    utc = dt.timezone.utc
    values = [
        dt.datetime(2026, 1, 2, 3, 4, 5, 600, tzinfo=utc),
        dt.datetime(2026, 1, 2, 3, 4, 5),
        dt.date(2026, 1, 2),
        dt.time(13, 30),
        dt.time(13, 30, 1, 500),
        dt.time(13, 30, tzinfo=utc),  # orjson's own encoder rejects this one
    ]
    assert json.loads(dumps(values)) == [v.isoformat() for v in values]


@pytest.mark.parametrize("dumps", BACKENDS)
def test_clean_values_pass_the_non_finite_check(dumps: Any) -> None:
    obj = {"a": [1, True, None, "x", 1.5, D("2"), (3,), {4}, frozenset({5})], "b": {"c": dt.date(2026, 1, 1)}}
    assert json.loads(dumps(obj))["a"][:5] == [1, True, None, "x", 1.5]


@pytest.mark.parametrize("dumps", BACKENDS)
@pytest.mark.parametrize(
    ("bad", "where"),
    [
        ([{"Id": "1", "x": 1.0}, {"Id": "2", "x": float("inf")}], r"inf at \$\[1\]\.x "),
        ({"ok": {"n": 1.0}, "bad": D("NaN")}, r"Decimal\('NaN'\) at \$\.bad "),
        ([[D("1"), "s"], [float("nan")]], r"nan at \$\[1\]\[0\] "),
    ],
)
def test_error_path_after_clean_siblings(dumps: Any, bad: Any, where: str) -> None:
    with pytest.raises(jsonlib.NonFiniteNumber, match=where):
        dumps(bad)


@pytest.mark.parametrize("dumps", BACKENDS)
@pytest.mark.parametrize("value", ["x", 1, True, None, 2.5, D("1.5"), dt.date(2026, 1, 1)])
def test_top_level_scalars(dumps: Any, value: Any) -> None:
    assert dumps(value) == jsonlib.std_dumps(value)
