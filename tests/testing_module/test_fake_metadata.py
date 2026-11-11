"""``fake.metadata``: the in-memory Metadata API of ``FakeSalesforce`` / ``AsyncFakeSalesforce``."""

from __future__ import annotations

import asyncio
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


ZIP = make_zip(
    {
        "package.xml": b"<Package/>",
        "classes/A.cls": b"public class A {}",
        "classes/A.cls-meta.xml": b"<ApexClass/>",
        "classes/B.cls": b"public class B {}",
    }
)


def test_deploy_succeeds_by_default_and_keeps_the_zip() -> None:
    fake = FakeSalesforce()
    job = fake.metadata.deploy(ZIP, options=esf.DeployOptions(check_only=True))
    assert job.id.startswith("0Af")
    assert (job.status, job.done, job.check_only) == ("Pending", False, True)
    result = fake.metadata.deploy_status(job.id, wait=True)
    assert (result.status, result.done, result.success, result.check_only) == ("Succeeded", True, True, True)
    assert result.components_total == result.components_deployed == 2
    result.raise_for_errors()
    assert fake.deployed_zip(job.id) == ZIP
    assert fake.deployed_zip(job.id[:15]) == ZIP
    assert fake.deployed_zip("0Afxx0000000009AAA") is None
    assert [c.operation for c in fake.calls] == ["metadata.deploy", "metadata.deploy_status"]
    assert fake.calls[0].args["size"] == len(ZIP)


def test_deploy_from_a_folder(tmp_path: Path) -> None:
    (tmp_path / "classes").mkdir()
    (tmp_path / "package.xml").write_bytes(b"<Package/>")
    (tmp_path / "classes" / "A.cls").write_bytes(b"public class A {}")
    fake = FakeSalesforce()
    job = fake.metadata.deploy(tmp_path)
    with zipfile.ZipFile(io.BytesIO(fake.deployed_zip(job.id) or b"")) as zf:
        assert sorted(zf.namelist()) == ["classes/A.cls", "package.xml"]
    zip_path = tmp_path / "x.zip"
    zip_path.write_bytes(ZIP)
    assert fake.deployed_zip(fake.metadata.deploy(str(zip_path)).id) == ZIP


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_deploy_refuses_what_the_client_refuses(tmp_path: Path) -> None:
    fake = FakeSalesforce()
    with pytest.raises(ValueError, match="not a zip"):
        fake.metadata.deploy(b"plain text")
    with pytest.raises(TypeError, match="zip bytes or a path"):
        fake.metadata.deploy(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"no package\.xml"):
        fake.metadata.deploy(tmp_path)
    (tmp_path / "package.xml").write_bytes(b"<Package/>")
    os.symlink("/etc/hosts", tmp_path / "hosts")
    with pytest.raises(ValueError, match="outside"):
        fake.metadata.deploy(tmp_path)
    assert fake.calls == []


def test_scripted_outcomes() -> None:
    fake = FakeSalesforce()
    failed = esf.DeployResult(
        "0Af000000000000",
        "Failed",
        done=True,
        component_failures=(esf.DeployMessage("ApexClass", "A", problem="Unexpected token"),),
    )
    fake.on_deploy(failed, times=1)
    fake.on_deploy(lambda data, options: esf.DeployResult("x", "Canceled", done=True), times=2)
    first = fake.metadata.deploy(ZIP)
    result = fake.metadata.deploy_status(first.id)
    assert result.id == first.id
    with pytest.raises(esf.DeployFailed, match="Unexpected token") as exc:
        result.raise_for_errors()
    assert exc.value.job_id == first.id
    assert [fake.metadata.deploy_status(fake.metadata.deploy(ZIP).id).status for _ in range(3)] == [
        "Canceled",
        "Canceled",
        "Succeeded",
    ]
    with pytest.raises(ValueError, match="times"):
        fake.on_deploy(failed, times=0)


def test_deploy_status_errors() -> None:
    fake = FakeSalesforce()
    with pytest.raises(esf.NotFound):
        fake.metadata.deploy_status("0Afxx0000000001AAA")
    with pytest.raises(ValueError):
        fake.metadata.deploy_status("bad id")
    with pytest.raises(ValueError, match="timeout"):
        fake.metadata.deploy_status("0Afxx0000000001AAA", wait=True, timeout=0)
    fake.fail_next("metadata.deploy", esf.RequestError("invalid zip", error_code="INVALID_ZIP"))
    with pytest.raises(esf.RequestError):
        fake.metadata.deploy(ZIP)


def test_retrieve_list_and_describe(tmp_path: Path) -> None:
    fake = FakeSalesforce()
    fake.add_metadata(
        "ApexClass", "A", {"classes/A.cls": "public class A {}", "classes/A.cls-meta.xml": "<ApexClass/>"}
    )
    fake.add_metadata("ApexClass", "B", {"classes/B.cls": b"public class B {}"})
    fake.add_metadata("Report", "Sales/Pipeline", {"reports/Sales/Pipeline.report": "<Report/>"})

    result = fake.metadata.retrieve({"ApexClass": ["A", "Missing"]}, extract_to=tmp_path / "out")
    assert result.id.startswith("09S")
    with zipfile.ZipFile(io.BytesIO(result.zip_file)) as zf:
        assert sorted(zf.namelist()) == ["classes/A.cls", "classes/A.cls-meta.xml", "package.xml"]
        assert b"<members>A</members>" in zf.read("package.xml")
    assert (tmp_path / "out" / "classes" / "A.cls").read_text() == "public class A {}"
    assert [p.full_name for p in result.file_properties] == ["A"]
    assert result.file_properties[0].file_name == "classes/A.cls"
    (msg,) = result.messages
    assert "Missing" in msg.problem

    every = fake.metadata.retrieve(
        "<Package><types><members>*</members><name>ApexClass</name></types></Package>"
    )
    assert [p.full_name for p in every.file_properties] == ["A", "B"]

    assert [p.full_name for p in fake.metadata.list_metadata("ApexClass")] == ["A", "B"]
    assert [p.full_name for p in fake.metadata.list_metadata("Report", folder="Sales")] == ["Sales/Pipeline"]
    assert fake.metadata.list_metadata("Report", folder="Other") == []

    describe = fake.metadata.describe_metadata()
    apex, report = describe.types
    assert apex == esf.MetadataType("ApexClass", "classes", "cls", in_folder=False, meta_file=True)
    assert (report.xml_name, report.in_folder, report.meta_file, report.suffix) == ("Report", True, False, "report")
    assert [c.operation for c in fake.calls] == [
        "metadata.retrieve",
        "metadata.retrieve",
        "metadata.list",
        "metadata.list",
        "metadata.list",
        "metadata.describe",
    ]


def test_seed_and_retrieve_validation() -> None:
    fake = FakeSalesforce()
    with pytest.raises(ValueError, match="type"):
        fake.add_metadata("Apex Class", "A", {"classes/A.cls": ""})
    with pytest.raises(ValueError, match="full_name"):
        fake.add_metadata("ApexClass", "", {"classes/A.cls": ""})
    with pytest.raises(ValueError, match="at least one"):
        fake.add_metadata("ApexClass", "A", {})
    with pytest.raises(ValueError, match="unsafe"):
        fake.add_metadata("ApexClass", "A", {"../A.cls": ""})
    with pytest.raises(ValueError, match="not a component file"):
        fake.add_metadata("ApexClass", "A", {"package.xml": ""})
    with pytest.raises(ValueError, match="no members"):
        fake.metadata.retrieve({"ApexClass": []})
    with pytest.raises(ValueError, match="timeout"):
        fake.metadata.retrieve({"ApexClass": ["*"]}, timeout=-1)
    with pytest.raises(ValueError):
        fake.metadata.list_metadata("not a type")
    assert fake.calls == []


def test_async_fake_metadata(tmp_path: Path) -> None:
    async def main() -> None:
        fake = AsyncFakeSalesforce()
        fake.add_metadata("ApexClass", "A", {"classes/A.cls": "public class A {}"})
        job = await fake.metadata.deploy(ZIP)
        assert (await fake.metadata.deploy_status(job.id, wait=True)).success
        result = await fake.metadata.retrieve({"ApexClass": ["*"]}, extract_to=tmp_path)
        assert (tmp_path / "package.xml").exists()
        assert result.file_properties[0].full_name == "A"
        assert [p.full_name for p in await fake.metadata.list_metadata("ApexClass")] == ["A"]
        assert (await fake.metadata.describe_metadata()).types[0].xml_name == "ApexClass"

    asyncio.run(main())


def test_views_share_metadata() -> None:
    fake = FakeSalesforce()
    job = fake.tagged("nightly").metadata.deploy(ZIP)
    assert fake.metadata.deploy_status(job.id).success
    assert fake.calls[0].caller == "nightly"
