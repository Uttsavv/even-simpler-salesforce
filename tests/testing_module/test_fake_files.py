"""``fake.files``: the in-memory Files area of ``FakeSalesforce`` / ``AsyncFakeSalesforce``."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

import even_simpler_salesforce as esf
from even_simpler_salesforce.testing import AsyncFakeSalesforce, FakeSalesforce

ACCOUNT = "001xx0000000001AAA"


def test_upload_download_and_list() -> None:
    fake = FakeSalesforce()
    cv = fake.files.upload(b"%PDF-1.7", filename="inv.pdf", link_to=ACCOUNT)
    assert cv.startswith("068")
    assert fake.file_content(cv) == b"%PDF-1.7"
    assert fake.files.download_bytes(cv) == b"%PDF-1.7"
    with fake.files.download(cv) as stream:
        assert b"".join(stream) == b"%PDF-1.7"
        assert stream.content_length == 8
        assert "8 bytes" in repr(stream)
    with pytest.raises(RuntimeError, match="closed"):
        list(stream)
    (link,) = fake.files.list_for(ACCOUNT)
    version = fake.record("ContentVersion", cv)
    assert version is not None
    assert version["FirstPublishLocationId"] == ACCOUNT
    assert link == esf.FileLink(
        link_id=link.link_id,
        content_document_id=version["ContentDocumentId"],
        latest_version_id=cv,
        title="inv.pdf",
        file_extension="pdf",
        size=8,
        share_type="V",
        visibility="AllUsers",
    )
    assert [c.operation for c in fake.calls] == [
        "files.upload",
        "files.download_bytes",
        "files.download",
        "files.list_for",
    ]
    assert fake.calls[0].args["size"] == 8


def test_sources_and_link(tmp_path: Path) -> None:
    fake = FakeSalesforce()
    path = tmp_path / "notes.TXT"
    path.write_bytes(b"hello")
    cv1 = fake.files.upload(path, title="Notes")
    cv2 = fake.files.upload(io.BytesIO(b"abc"), filename="a.bin")
    cv3 = fake.files.upload(iter([b"x", bytearray(b"y")]), filename="s")
    assert [fake.file_content(c) for c in (cv1, cv2, cv3)] == [b"hello", b"abc", b"xy"]
    v1 = fake.record("ContentVersion", cv1)
    assert v1 is not None
    assert (v1["Title"], v1["PathOnClient"], v1["FileExtension"]) == ("Notes", "notes.TXT", "txt")
    v3 = fake.record("ContentVersion", cv3)
    assert v3 is not None
    assert v3["FileExtension"] is None
    doc = v1["ContentDocumentId"]
    link_id = fake.files.link(doc, ACCOUNT, share_type="C", visibility="InternalUsers")
    assert link_id.startswith("06A")
    (link,) = fake.files.list_for(ACCOUNT)
    assert (link.share_type, link.visibility) == ("C", "InternalUsers")
    fake.files.upload(b"z", filename="z.txt", link_to=ACCOUNT, share_type="I")
    assert sorted(x.share_type or "" for x in fake.files.list_for(ACCOUNT)) == ["C", "I"]
    assert fake.files.list_for("001xx0000000002AAA") == []


def test_validation_matches_real_client() -> None:
    fake = FakeSalesforce()
    with pytest.raises(ValueError, match="filename"):
        fake.files.upload(b"x")
    with pytest.raises(ValueError, match="link_to"):
        fake.files.upload(b"x", filename="a", visibility="AllUsers")
    with pytest.raises(ValueError, match="share_type"):
        fake.files.upload(b"x", filename="a", link_to=ACCOUNT, share_type="Q")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="visibility"):
        fake.files.upload(b"x", filename="a", link_to=ACCOUNT, visibility="All")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        fake.files.upload(b"x", filename="a", link_to="nope")
    with pytest.raises(TypeError, match="upload\\(\\) takes"):
        fake.files.upload(3, filename="a")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must yield bytes"):
        fake.files.upload(iter(["s"]), filename="a")  # type: ignore[list-item]
    with pytest.raises(TypeError, match="binary mode"):
        fake.files.upload(io.StringIO("s"), filename="a")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="visibility"):
        fake.files.link("069xx0000000001AAA", ACCOUNT, visibility="x")  # type: ignore[arg-type]
    assert fake.calls == []


def test_errors_and_injected_failures() -> None:
    fake = FakeSalesforce()
    with pytest.raises(esf.NotFound):
        fake.files.download_bytes("068xx0000000009AAA")
    with pytest.raises(esf.InvalidReference) as ei:
        fake.files.link("069xx0000000009AAA", ACCOUNT)
    assert ei.value.error_code == "INVALID_CROSS_REFERENCE_KEY"
    fake.fail_next("files.upload", esf.StorageLimitExceeded("storage limit exceeded"))
    with pytest.raises(esf.StorageLimitExceeded) as si:
        fake.files.upload(b"x", filename="a.txt")
    assert si.value.operation == "files.upload"
    assert fake.records("ContentVersion") == []


def test_seed_files_and_attachments() -> None:
    fake = FakeSalesforce()
    cv = fake.add_file(b"seeded", filename="/tmp/s.csv", link_to=ACCOUNT)
    assert fake.calls == []
    assert fake.file_content(cv) == b"seeded"
    assert fake.file_content("068xx0000000009AAA") is None
    assert [x.latest_version_id for x in fake.files.list_for(ACCOUNT)] == [cv]
    plain = fake.add_file(b"x", filename="x.txt")
    assert fake.files.download_bytes(plain) == b"x"
    att = fake.add("Attachment", {"Name": "a.pdf", "Body": b"%PDF"})
    txt = fake.add("Attachment", {"Name": "a.txt", "Body": "text"})
    empty = fake.add("Attachment", {"Name": "e"})
    assert fake.files.download_attachment(att) == b"%PDF"
    assert fake.files.download_attachment(txt) == b"text"
    assert fake.files.download_attachment(empty) == b""


def test_async_fake_files() -> None:
    async def main() -> None:
        async with AsyncFakeSalesforce() as fake:

            async def chunks() -> AsyncIterator[bytes]:
                yield b"as"
                yield b"ync"

            cv = await fake.files.upload(chunks(), filename="a.bin", link_to=ACCOUNT)
            assert await fake.files.download_bytes(cv) == b"async"
            async with fake.files.download(cv) as stream:
                assert [c async for c in stream] == [b"async"]
                assert stream.content_length == 5
                assert "5 bytes" in repr(stream)
            await stream.aclose()
            (link,) = await fake.files.list_for(ACCOUNT)
            await fake.files.link(link.content_document_id, "001xx0000000002AAA", visibility="AllUsers")
            assert len(await fake.files.list_for("001xx0000000002AAA")) == 1
            att = fake.add("Attachment", {"Body": b"b"})
            assert await fake.files.download_attachment(att) == b"b"
            assert await fake.files.upload(b"x", filename="b.txt")

    asyncio.run(main())


def test_views_share_files() -> None:
    fake = FakeSalesforce()
    cv = fake.tagged("billing").files.upload(b"x", filename="a.txt")
    assert fake.files.download_bytes(cv) == b"x"
    assert fake.calls[0].caller == "billing"

    def gen() -> Iterator[bytes]:
        yield b"q"

    assert fake.file_content(fake.files.upload(gen(), filename="q")) == b"q"
