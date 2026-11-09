"""sObject Collections send the right number of requests, of the right size, and keep order.

simple-salesforce regressions this guards against (behaviour only, no code was read or
copied): #590, #693, #739 (more than 200 records in one Collections request, or results not
lined up with the input) and #280 (an empty list still made an API call).
"""

from __future__ import annotations

import gzip
import json
import threading
from typing import Any

import httpx
import pytest
import respx

from even_simpler_salesforce.core.records import COLLECTION_READ_CHUNK, COLLECTION_WRITE_CHUNK

from ..conftest import DATA, Harness

SIZES = [0, 1, 199, 200, 201, 400, 401, 2000, 2001]


def rid(n: int) -> str:
    return f"001xx{n:013d}"


def body_of(request: httpx.Request) -> Any:
    raw = request.content
    if request.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def expected_sizes(n: int, size: int) -> list[int]:
    return [min(size, n - i) for i in range(0, n, size)]


class Echo:
    """Answers each chunk with one result per record (id = the record's own id), records sizes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sizes: list[int] = []

    def write(self, request: httpx.Request) -> httpx.Response:
        records = body_of(request)["records"]
        with self.lock:
            self.sizes.append(len(records))
        return httpx.Response(
            200, json=[{"id": r["Id"], "success": True, "errors": [], "created": False} for r in records]
        )

    def delete(self, request: httpx.Request) -> httpx.Response:
        ids = request.url.params["ids"].split(",")
        with self.lock:
            self.sizes.append(len(ids))
        return httpx.Response(200, json=[{"id": i, "success": True, "errors": []} for i in ids])

    def retrieve(self, request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        with self.lock:
            self.sizes.append(len(body["ids"]))
        return httpx.Response(200, json=[{"Id": i, "attributes": {"type": "Account"}} for i in body["ids"]])


def rows(n: int) -> list[dict[str, Any]]:
    return [{"Id": rid(i), "Name": f"n{i}"} for i in range(n)]


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("op", ["create", "update", "upsert", "delete"])
def test_writes(h: Harness, mock: respx.MockRouter, n: int, op: str) -> None:
    echo = Echo()
    write = mock.route(url__startswith=f"{DATA}/composite/sobjects", method__in=["POST", "PATCH"])
    write.mock(side_effect=echo.write)
    mock.delete(url__startswith=f"{DATA}/composite/sobjects").mock(side_effect=echo.delete)
    sf = h.client()
    data = rows(n)
    if op == "create":
        res = h.run(sf.collections.create(data, sobject="Account"))
    elif op == "update":
        res = h.run(sf.collections.update("Account", data))
    elif op == "upsert":
        res = h.run(sf.collections.upsert("Account", "Id", data))
    else:
        res = h.run(sf.collections.delete([r["Id"] for r in data]))
    assert sorted(echo.sizes) == sorted(expected_sizes(n, COLLECTION_WRITE_CHUNK))
    assert len(mock.calls) == math_ceil(n, COLLECTION_WRITE_CHUNK)
    assert [r.id for r in res] == [rid(i) for i in range(n)]  # input order, whatever the concurrency


@pytest.mark.parametrize("n", SIZES)
def test_retrieve(h: Harness, mock: respx.MockRouter, n: int) -> None:
    echo = Echo()
    mock.post(f"{DATA}/composite/sobjects/Account").mock(side_effect=echo.retrieve)
    sf = h.client()
    ids = [rid(i) for i in range(n)]
    res = h.run(sf.collections.retrieve("Account", ids, ["Id", "Name"]))
    assert sorted(echo.sizes) == sorted(expected_sizes(n, COLLECTION_READ_CHUNK))
    assert len(mock.calls) == math_ceil(n, COLLECTION_READ_CHUNK)
    assert [r["Id"] for r in res] == ids


def math_ceil(n: int, size: int) -> int:
    return -(-n // size)
