from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture


def rid(prefix: str, n: int) -> str:
    return f"{prefix}xx{n:013d}"


def body_of(request: httpx.Request) -> Any:
    raw = request.content
    if request.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def echo_write(request: httpx.Request) -> httpx.Response:
    """Succeed for every record, echoing a per-row id derived from the row's Name/Id."""
    records = body_of(request)["records"]
    out = [
        {"id": r.get("Id") or r["Name"], "success": True, "errors": [], "created": "Id" not in r}
        for r in records
    ]
    return httpx.Response(200, json=out)


def test_create_chunks_of_200_keep_order(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.post(f"{DATA}/composite/sobjects").mock(side_effect=echo_write)
    sf = h.client(recorder=rec)
    rows = [{"Name": f"n{i:04d}"} for i in range(450)]
    res = h.run(sf.collections.create(rows, sobject="Account"))
    assert isinstance(res, esf.BatchResult)
    assert [r.id for r in res] == [f"n{i:04d}" for i in range(450)]
    assert all(r.ok and r.created for r in res)
    sizes = sorted(len(body_of(c.request)["records"]) for c in route.calls)
    assert sizes == [50, 200, 200]
    first = body_of(route.calls[0].request)
    assert first["allOrNone"] is False
    assert first["records"][0]["attributes"] == {"type": "Account"}
    assert res.operation == "collections.create"
    assert res.sobject == "Account"
    assert sorted(e["records"] for e in rec.requests) == [50, 200, 200]
    assert {e["operation"] for e in rec.requests} == {"collections.create"}


def test_create_order_preserved_with_concurrency(h: Harness, mock: respx.MockRouter) -> None:
    """Later chunks answer first; results still follow the input order."""

    def slow_first(request: httpx.Request) -> httpx.Response:
        names = [r["Name"] for r in body_of(request)["records"]]
        if names[0] == "n0000" and not h.is_async:
            time.sleep(0.05)
        return echo_write(request)

    mock.post(f"{DATA}/composite/sobjects").mock(side_effect=slow_first)
    sf = h.client()
    rows = [{"Name": f"n{i:04d}", "attributes": {"type": "Lead"}} for i in range(800)]
    res = h.run(sf.collections.create(rows, concurrency=4))
    assert [r.id for r in res] == [f"n{i:04d}" for i in range(800)]


def test_create_runs_chunks_concurrently_up_to_limit(h: Harness, mock: respx.MockRouter) -> None:
    if not h.is_async:
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def track(request: httpx.Request) -> httpx.Response:
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.02)
            with lock:
                state["now"] -= 1
            return echo_write(request)

        mock.post(f"{DATA}/composite/sobjects").mock(side_effect=track)
        sf = h.client()
        h.run(sf.collections.create([{"Name": f"n{i}"} for i in range(1000)], sobject="Lead", concurrency=3))
        assert 1 < state["peak"] <= 3
    else:
        mock.post(f"{DATA}/composite/sobjects").mock(side_effect=echo_write)
        sf = h.client()
        res = h.run(sf.collections.create([{"Name": f"n{i}"} for i in range(1000)], sobject="Lead"))
        assert len(res) == 1000


def test_create_needs_type(h: Harness, mock: respx.MockRouter) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="attributes.type"):
        h.run(sf.collections.create([{"Name": "x"}]))
    assert h.run(sf.collections.create([])).results == []
    assert not mock.calls


def test_partial_failure_via_raise_for_errors(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/composite/sobjects").respond(json=load_fixture("collections/mixed.json"))
    sf = h.client()
    res = h.run(sf.collections.create([{"Name": "ok"}, {"Name": ""}], sobject="Account"))
    assert len(res.succeeded) == 1
    assert len(res.failed) == 1
    assert res.failed[0].errors[0].error_code == "REQUIRED_FIELD_MISSING"
    with pytest.raises(esf.PartialFailure) as ei:
        res.raise_for_errors()
    assert ei.value.results is res
    assert ei.value.error_code == "REQUIRED_FIELD_MISSING"
    assert ei.value.fields == ("Name",)


def test_update_requires_id_and_sends_patch(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.patch(f"{DATA}/composite/sobjects").mock(side_effect=echo_write)
    sf = h.client()
    with pytest.raises(ValueError, match="Id"):
        h.run(sf.collections.update("Lead", [{"Status": "x"}]))
    ids = [rid("00Q", i) for i in range(201)]
    res = h.run(sf.collections.update("Lead", [{"Id": i, "Status": "x"} for i in ids]))
    assert [r.id for r in res] == ids
    assert route.call_count == 2
    with pytest.raises(ValueError):
        h.run(sf.collections.update("Bad Name", [{"Id": ids[0]}]))


def test_upsert_path_and_no_resend_after_timeout(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.patch(f"{DATA}/composite/sobjects/Account/Ext__c")
    route.side_effect = [httpx.ReadTimeout("slow")]
    sf = h.client()
    # ADR 0001: a resend after a create would report created=False and re-run triggers.
    with pytest.raises(esf.RequestTimeout):
        h.run(sf.collections.upsert("Account", "Ext__c", [{"Ext__c": "1", "Name": "x"}]))
    assert route.call_count == 1
    with pytest.raises(ValueError):
        h.run(sf.collections.upsert("Account", "bad field", [{"x": 1}]))


def test_create_post_timeout_is_not_retried(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/composite/sobjects").mock(side_effect=httpx.ReadTimeout("slow"))
    sf = h.client()
    with pytest.raises(esf.RequestTimeout):
        h.run(sf.collections.create([{"Name": "x"}], sobject="Account"))
    assert route.call_count == 1


def test_delete_chunks(h: Harness, mock: respx.MockRouter) -> None:
    def echo_delete(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["ids"].split(",")
        return httpx.Response(200, json=[{"id": i, "success": True, "errors": []} for i in ids])

    route = mock.delete(f"{DATA}/composite/sobjects").mock(side_effect=echo_delete)
    sf = h.client()
    ids = [rid("001", i) for i in range(401)]
    res = h.run(sf.collections.delete(ids))
    assert [r.id for r in res] == ids
    assert sorted(len(c.request.url.params["ids"].split(",")) for c in route.calls) == [1, 200, 200]
    assert {c.request.url.params["allOrNone"] for c in route.calls} == {"false"}
    assert h.run(sf.collections.delete([])).results == []
    with pytest.raises(ValueError):
        h.run(sf.collections.delete(["bad id"]))


def test_retrieve_chunks_of_2000(h: Harness, mock: respx.MockRouter) -> None:
    def echo_retrieve(request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        assert body["fields"] == ["Id", "Name"]
        return httpx.Response(200, json=[{"Id": i} if not i.endswith("7") else None for i in body["ids"]])

    route = mock.post(f"{DATA}/composite/sobjects/Lead").mock(side_effect=echo_retrieve)
    sf = h.client()
    ids = [rid("00Q", i) for i in range(4500)]
    out = h.run(sf.collections.retrieve("Lead", ids, ["Id", "Name"]))
    assert len(out) == 4500
    assert [o["Id"] if o else None for o in out] == [i if not i.endswith("7") else None for i in ids]
    assert sorted(len(body_of(c.request)["ids"]) for c in route.calls) == [500, 2000, 2000]
    # 2000 ids is > 64 KiB? No: bodies are small; gzip only above the threshold.
    assert all(c.request.headers.get("Content-Encoding") in (None, "gzip") for c in route.calls)
    with pytest.raises(ValueError):
        h.run(sf.collections.retrieve("Lead", ids, []))


def test_retrieve_bad_shape(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/composite/sobjects/Lead").respond(json=[])
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.collections.retrieve("Lead", [rid("00Q", 1)], ["Id"]))


def test_large_write_bodies_are_gzipped(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(f"{DATA}/composite/sobjects").mock(side_effect=echo_write)
    sf = h.client()
    rows = [{"Name": f"n{i:03d}", "Description": "x" * 500} for i in range(200)]
    h.run(sf.collections.create(rows, sobject="Account"))
    req = route.calls.last.request
    assert req.headers["Content-Encoding"] == "gzip"
    assert len(body_of(req)["records"]) == 200
    route.reset()
    h.run(sf.collections.create([{"Name": "small"}], sobject="Account"))
    assert "Content-Encoding" not in route.calls.last.request.headers


# --- all_or_none --------------------------------------------------------------------------------


def test_all_or_none_runs_sequentially_and_raises_typed(
    h: Harness, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        assert body["allOrNone"] is True
        calls.append(len(body["records"]))
        if len(calls) == 2:  # second chunk fails atomically
            out = [
                {
                    "success": False,
                    "errors": [{"statusCode": "ALL_OR_NONE_OPERATION_ROLLED_BACK", "message": "rb"}],
                }
                for _ in body["records"]
            ]
            out[5] = {
                "success": False,
                "errors": [
                    {
                        "statusCode": "FIELD_CUSTOM_VALIDATION_EXCEPTION",
                        "message": "[ADDR_REQUIRED] Street is required",
                        "fields": ["Street"],
                    }
                ],
            }
            return httpx.Response(200, json=out)
        return echo_write(request)

    class AddressRequired(esf.ValidationRuleFailed):
        pass

    mock.post(f"{DATA}/composite/sobjects").mock(side_effect=handler)
    sf = h.client(error_tags={"ADDR_REQUIRED": AddressRequired})
    rows = [{"Name": f"n{i:03d}"} for i in range(600)]
    with caplog.at_level(logging.WARNING, logger="esf"), pytest.raises(AddressRequired) as ei:
        h.run(sf.collections.create(rows, sobject="Account", all_or_none=True, concurrency=8))
    assert calls == [200, 200]  # sequential: third chunk never sent
    exc = ei.value
    assert exc.message == "Street is required"
    assert exc.fields == ("Street",)
    assert exc.operation == "collections.create"
    assert {d.error_code for d in exc.errors} == {
        "ALL_OR_NONE_OPERATION_ROLLED_BACK",
        "FIELD_CUSTOM_VALIDATION_EXCEPTION",
    }
    assert any("earlier chunks" in r.getMessage() for r in caplog.records)


def test_all_or_none_only_rolled_back_details(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/composite/sobjects").respond(
        json=[
            {
                "success": False,
                "errors": [{"statusCode": "ALL_OR_NONE_OPERATION_ROLLED_BACK", "message": "rb"}],
            }
        ]
    )
    sf = h.client()
    with pytest.raises(esf.RecordError) as ei:
        h.run(sf.collections.create([{"Name": "x"}], sobject="Account", all_or_none=True))
    assert ei.value.error_code == "ALL_OR_NONE_OPERATION_ROLLED_BACK"


def test_all_or_none_failure_without_details(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(f"{DATA}/composite/sobjects").respond(json=[{"success": False, "errors": []}])
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse):
        h.run(sf.collections.create([{"Name": "x"}], sobject="Account", all_or_none=True))


def test_all_or_none_success_and_small_batch_no_warning(
    h: Harness, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    mock.post(f"{DATA}/composite/sobjects").mock(side_effect=echo_write)
    sf = h.client()
    with caplog.at_level(logging.WARNING, logger="esf"):
        res = h.run(sf.collections.create([{"Name": "a"}], sobject="Account", all_or_none=True))
    assert res[0].ok
    assert not caplog.records


def test_delete_all_or_none(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.delete(f"{DATA}/composite/sobjects").respond(
        json=[{"success": False, "errors": [{"statusCode": "ENTITY_IS_DELETED", "message": "gone"}]}]
    )
    sf = h.client()
    with pytest.raises(esf.NotFound):
        h.run(sf.collections.delete([rid("001", 1)], all_or_none=True))
    assert route.calls.last.request.url.params["allOrNone"] == "true"


def test_rolled_back_fixture(h: Harness, mock: respx.MockRouter) -> None:
    mock.patch(f"{DATA}/composite/sobjects").respond(json=load_fixture("collections/rolled_back.json"))
    sf = h.client()
    rows = [{"Id": rid("001", 1)}, {"Id": rid("001", 2)}]
    with pytest.raises(esf.ValidationRuleFailed) as ei:
        h.run(sf.collections.update("Account", rows, all_or_none=True))
    assert ei.value.tag == "ADDR_REQUIRED"
    res = h.run(sf.collections.update("Account", rows))
    assert len(res.failed) == 2
