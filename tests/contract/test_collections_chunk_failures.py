"""A chunk whose request fails must not hide what the other chunks committed (review M1).

Both clients wait for every chunk before raising, so no write is left running in the
background, and the caller gets a ``PartialFailure`` carrying the full ordered ``BatchResult``.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness

URL = f"{DATA}/composite/sobjects"


def _ok(request: httpx.Request) -> httpx.Response:
    records = json.loads(request.content)["records"]
    return httpx.Response(
        200, json=[{"id": f"001{i:015d}", "success": True, "errors": []} for i, _ in enumerate(records)]
    )


def test_failed_chunk_reported_after_all_chunks_finish(h: Harness, mock: respx.MockRouter) -> None:
    calls: list[int] = []
    lock = threading.Lock()

    def endpoint(request: httpx.Request) -> httpx.Response:
        first = json.loads(request.content)["records"][0]["Name"]
        with lock:
            calls.append(first)
        if first == 0:
            return httpx.Response(500, json=[{"errorCode": "SERVER_ERROR", "message": "boom"}])
        return _ok(request)

    mock.post(URL).mock(side_effect=endpoint)
    sf = h.client()
    rows = [{"Name": i} for i in range(600)]
    with pytest.raises(esf.PartialFailure) as ei:
        h.run(sf.collections.create(rows, sobject="Account"))
    assert sorted(calls) == [0, 200, 400]  # every chunk was sent and finished before the raise
    batch = ei.value.results
    assert batch is not None
    assert len(batch) == 600
    assert [r.ok for r in batch][:200] == [False] * 200
    assert all(r.ok for r in batch.results[200:])
    detail = batch[0].errors[0]
    assert detail.extended["outcome"] == "unknown"  # a 5xx on POST may or may not have written
    assert detail.extended["chunk_error"] == "ServerError"
    assert isinstance(ei.value.__cause__, esf.ServerError)
    assert ei.value.operation == "collections.create"


def test_all_chunks_failing_raises_the_original_error(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(URL).respond(403, json=[{"errorCode": "INSUFFICIENT_ACCESS", "message": "no"}])
    sf = h.client()
    with pytest.raises(esf.Forbidden):
        h.run(sf.collections.create([{"Name": i} for i in range(300)], sobject="Account"))


def test_retrieve_chunk_failure_raises_original(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{URL}/Account")
    route.side_effect = [
        httpx.Response(200, json=[{"Id": "x"}] * 2000),
        httpx.Response(400, json=[{"errorCode": "INVALID_FIELD", "message": "bad"}]),
    ]
    sf = h.client(retry=esf.RetryPolicy(max_attempts=1))
    ids = [f"001{i:015d}" for i in range(2001)]
    with pytest.raises(esf.InvalidField):
        h.run(sf.collections.retrieve("Account", ids, ["Id"], concurrency=1))


@pytest.mark.parametrize(
    ("call", "method"),
    [
        (
            lambda sf: sf.collections.update("Account", [{"Id": "001000000000001AAA"}], all_or_none=True),
            "PATCH",
        ),
        (lambda sf: sf.collections.upsert("Account", "Ext__c", [{"Ext__c": "1"}], all_or_none=True), "PATCH"),
        (lambda sf: sf.collections.delete(["001000000000001AAA"], all_or_none=True), "DELETE"),
        (lambda sf: sf.collections.create([{"Name": "a"}], sobject="Account", all_or_none=True), "POST"),
    ],
)
def test_all_or_none_error_reports_real_method(
    h: Harness, mock: respx.MockRouter, call: Any, method: str
) -> None:
    body = [
        {"success": False, "errors": [{"statusCode": "REQUIRED_FIELD_MISSING", "message": "x", "fields": []}]}
    ]
    mock.route(url__startswith=URL).respond(json=body)
    sf = h.client()
    with pytest.raises(esf.RequiredFieldMissing) as ei:
        h.run(call(sf))
    assert ei.value.method == method
