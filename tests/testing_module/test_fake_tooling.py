"""``fake.tooling``: the Tooling API of ``FakeSalesforce`` / ``AsyncFakeSalesforce`` (§9.12, §14)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce, UnscriptedCall


def test_query_and_query_more_use_tooling_operations() -> None:
    fake = FakeSalesforce()
    rows = [{"Id": f"01pxx{i:010d}AAA", "Name": f"C{i}"} for i in range(2001)]
    fake.on_query("SELECT Id, Name FROM ApexClass", rows=rows)
    page = fake.tooling.query("SELECT Id, Name FROM ApexClass")
    assert len(page.records) == 2000
    assert page.total_size == 2001
    assert page.cursor is not None
    assert page.records[0]["attributes"]["type"] == "ApexClass"
    nxt = fake.query_more(page.cursor)
    assert [r["Name"] for r in nxt] == ["C2000"]
    assert nxt.cursor is None
    assert [c.operation for c in fake.calls] == ["tooling.query", "tooling.query_more"]
    assert fake.calls[0].sobject == "ApexClass"


def test_query_iter_and_model() -> None:
    from dataclasses import dataclass

    @dataclass
    class Cls:
        Id: str
        Name: str

    fake = FakeSalesforce()
    rows = [{"Id": f"01pxx{i:010d}AAA", "Name": f"C{i}"} for i in range(2001)]
    fake.on_query(re.compile("FROM ApexClass"), rows=rows)
    got = list(fake.tooling.query_iter("SELECT Id, Name FROM ApexClass WHERE Name LIKE {}", "C%", model=Cls))
    assert len(got) == 2001
    assert got[-1] == Cls("01pxx0000002000AAA", "C2000")
    assert [c.operation for c in fake.calls] == ["tooling.query", "tooling.query_more"]
    assert fake.calls[0].args["soql"] == "SELECT Id, Name FROM ApexClass WHERE Name LIKE 'C%'"
    assert isinstance(fake.tooling.query("SELECT Id, Name FROM ApexClass", model=Cls).records[0], Cls)


def test_sobject_uses_tooling_operations_and_the_record_store() -> None:
    fake = FakeSalesforce()
    cls = fake.tooling.sobject("ApexClass")
    cid = cls.create({"Name": "Tmp", "Body": "public class Tmp {}"})
    assert cid.startswith("01p")
    assert cls.get(cid)["Name"] == "Tmp"
    cls.update(cid, {"Body": "public class Tmp { }"})
    assert fake.record("ApexClass", cid)["Body"] == "public class Tmp { }"
    cls.delete(cid)
    assert [c.operation for c in fake.calls] == [
        "tooling.sobject.create",
        "tooling.sobject.get",
        "tooling.sobject.update",
        "tooling.sobject.delete",
    ]
    fake.fail_next("tooling.sobject.create", esf.Forbidden("no"))
    with pytest.raises(esf.Forbidden):
        cls.create({"Name": "X"})
    fake.sobject("ApexClass").create({"Name": "Y"})  # the REST twin keeps its own operation name
    assert fake.calls[-1].operation == "sobject.create"


def test_execute_anonymous_scripts() -> None:
    fake = FakeSalesforce()
    with pytest.raises(UnscriptedCall, match="on_execute_anonymous"):
        fake.tooling.execute_anonymous("System.debug(1);")
    ok = fake.on_execute_anonymous("System.debug(1);")
    fake.on_execute_anonymous(
        re.compile(r"1 / 0"),
        esf.AnonymousResult(
            compiled=True, success=False, exception_message="System.MathException: Divide by 0"
        ),
    )
    fake.on_execute_anonymous(
        lambda code: "log" in code,
        lambda code: esf.AnonymousResult(compiled=True, success=True, log=f"USER_DEBUG|{code}"),
    )
    fake.on_execute_anonymous("boom;", esf.ServerError("down"), times=1)
    assert fake.tooling.execute_anonymous("System.debug(1);") == esf.AnonymousResult(
        compiled=True, success=True
    )
    assert ok.hits == 1
    assert fake.tooling.execute_anonymous("Integer i = 1 / 0;").exception_message
    assert fake.tooling.execute_anonymous("// log", capture_log=True).log == "USER_DEBUG|// log"
    assert fake.tooling.execute_anonymous("// log").log is None  # only with capture_log=True
    with pytest.raises(esf.ServerError):
        fake.tooling.execute_anonymous("boom;")
    with pytest.raises(UnscriptedCall):
        fake.tooling.execute_anonymous("boom;")  # times=1 used up
    with pytest.raises(ValueError):
        fake.tooling.execute_anonymous(" ")
    calls = fake.calls_for("tooling.execute_anonymous")
    assert calls[3].args == {"code": "// log", "capture_log": True}
    assert isinstance(calls[-1].error, UnscriptedCall)


def test_execute_anonymous_bad_scripts() -> None:
    fake = FakeSalesforce()
    with pytest.raises(TypeError, match="AnonymousResult"):
        fake.on_execute_anonymous("x;", {"compiled": True})
    fake.on_execute_anonymous("x;", lambda code: {"compiled": True})
    with pytest.raises(TypeError, match="AnonymousResult"):
        fake.tooling.execute_anonymous("x;")


def test_run_tests_async_and_completions() -> None:
    fake = FakeSalesforce()
    job = fake.tooling.run_tests_async(["01pxx0000000002AAA", "01pxx0000000004AAA"])
    assert job.startswith("707")
    assert fake.calls[0].args == {"class_ids": ["01pxx0000000002AAA", "01pxx0000000004AAA"]}
    with pytest.raises(TypeError):
        fake.tooling.run_tests_async("01pxx0000000002AAA")
    assert fake.tooling.completions("apex") == {}
    fake.set_completions("apex", {"publicDeclarations": {"System": {"Math": []}}})
    out = fake.tooling.completions("apex")
    out["publicDeclarations"].clear()  # callers get a copy
    assert fake.tooling.completions("apex") == {"publicDeclarations": {"System": {"Math": []}}}
    with pytest.raises(ValueError):
        fake.tooling.completions("java")
    with pytest.raises(ValueError):
        fake.set_completions("java", {})


def test_views_share_the_tooling_state() -> None:
    fake = FakeSalesforce()
    fake.on_execute_anonymous("x;")
    tagged = fake.tagged("job")
    assert tagged.tooling.execute_anonymous("x;").success
    assert fake.calls[-1].caller == "job"


def test_async_fake() -> None:
    async def collect(it: AsyncIterator[Any]) -> list[Any]:
        return [r async for r in it]

    async def main() -> None:
        fake = AsyncFakeSalesforce()
        fake.on_query("SELECT Id FROM ApexClass", rows=[{"Id": "01pxx0000000001AAA"}])
        fake.on_execute_anonymous("x;", esf.AnonymousResult(compiled=True, success=True, log="L"))
        fake.set_completions("visualforce", {"publicDeclarations": {}})
        assert (await fake.tooling.query("SELECT Id FROM ApexClass")).total_size == 1
        assert len(await collect(fake.tooling.query_iter("SELECT Id FROM ApexClass"))) == 1
        cid = await fake.tooling.sobject("ApexClass").create({"Name": "A"})
        assert (await fake.tooling.sobject("ApexClass").get(cid))["Name"] == "A"
        assert (await fake.tooling.execute_anonymous("x;", capture_log=True)).log == "L"
        assert (await fake.tooling.run_tests_async(["01pxx0000000002AAA"])).startswith("707")
        assert await fake.tooling.completions("visualforce") == {"publicDeclarations": {}}

    asyncio.run(main())


def test_describe_global_lists_known_objects() -> None:
    fake = FakeSalesforce()
    fake.tooling.sobject("ApexClass").create({"Name": "C"})
    got = fake.tooling.describe_global(cached=False)
    assert "ApexClass" in [o["name"] for o in got["sobjects"]]
    (call,) = fake.calls_for("tooling.describe_global")
    assert call.args == {"cached": False}


def test_async_describe_global() -> None:
    async def main() -> None:
        fake = AsyncFakeSalesforce()
        got = await fake.tooling.describe_global()
        assert "sobjects" in got

    asyncio.run(main())
