"""``sf.files`` (§9.9) against hand-written responses, for both clients.

All fixtures under ``tests/fixtures/recorded/files`` are **synthetic** (no org to record from).
Salesforce behaviour relied on:

- *Insert or Update Blob Data*, REST API Developer Guide
  (https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/dome_sobject_insert_update_blob.htm):
  multipart ``POST sobjects/ContentVersion`` with an ``entity_content`` JSON part and a
  ``VersionData`` part that carries a ``filename``; up to 2 GB per ContentVersion; the response
  is the usual ``{"id", "success", "errors"}``.
- *Get Blob Data*: ``GET sobjects/ContentVersion/{id}/VersionData`` and
  ``GET sobjects/Attachment/{id}/Body`` return the raw bytes.
- *ContentDocumentLink*, Object Reference
  (https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_contentdocumentlink.htm):
  ``ShareType`` V/C/I, ``Visibility`` AllUsers/InternalUsers/SharedUsers; a query filters on
  ``LinkedEntityId``.
"""

from __future__ import annotations

import io
import json
import re
import tracemalloc
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import even_simpler_salesforce as esf

from ..conftest import DATA, Harness, Recorder
from ..fixtures import load_fixture

CV_ID = "068xx0000000001AAA"
DOC_ID = "069xx0000000001AAA"
LINK_ID = "06Axx0000000001AAA"
ACCOUNT_ID = "001xx0000000001AAA"
ATT_ID = "00Pxx0000000001AAA"
CV_URL = f"{DATA}/sobjects/ContentVersion"
LINK_URL = f"{DATA}/sobjects/ContentDocumentLink"
CHUNK = 256 * 1024


def created(name: str = "files/version_created.json") -> httpx.Response:
    return httpx.Response(201, json=load_fixture(name))


def parts(request: httpx.Request) -> tuple[dict[str, Any], dict[str, str], bytes]:
    """Split a multipart upload into (entity JSON, VersionData part headers, file bytes)."""
    m = re.fullmatch(r'multipart/form-data; boundary="([^"]+)"', request.headers["content-type"])
    assert m, request.headers["content-type"]
    boundary = m.group(1).encode()
    body = request.content
    assert body.endswith(b"\r\n--" + boundary + b"--\r\n")
    chunks = body.split(b"--" + boundary)
    assert chunks[0] == b""
    assert chunks[-1] == b"--\r\n"
    entity_part, file_part = chunks[1], chunks[2]
    e_head, e_body = entity_part.split(b"\r\n\r\n", 1)
    assert b'Content-Disposition: form-data; name="entity_content"' in e_head
    assert b"Content-Type: application/json" in e_head
    f_head, f_body = file_part.split(b"\r\n\r\n", 1)
    headers = dict(line.split(": ", 1) for line in f_head.decode().strip().split("\r\n"))
    return json.loads(e_body.removesuffix(b"\r\n")), headers, f_body.removesuffix(b"\r\n")


def stream_of(h: Harness, chunks: list[Any]) -> Any:
    if h.is_async:

        async def agen() -> AsyncIterator[bytes]:
            for c in chunks:
                yield c

        return agen()

    def gen() -> Iterator[bytes]:
        yield from chunks

    return gen()


# --- upload ---------------------------------------------------------------------------------


def test_upload_bytes_is_multipart(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.post(CV_URL).mock(return_value=created())
    sf = h.client(recorder=rec)
    data = b"%PDF-1.7 fake" * 1000
    assert h.run(sf.files.upload(data, filename="inv.pdf", title="Invoice")) == CV_ID
    req = route.calls.last.request
    entity, headers, content = parts(req)
    assert entity == {"PathOnClient": "inv.pdf", "Title": "Invoice"}
    assert headers["Content-Disposition"] == 'form-data; name="VersionData"; filename="inv.pdf"'
    assert headers["Content-Type"] == "application/octet-stream"
    assert content == data
    assert req.headers["content-length"] == str(len(req.content))
    assert "transfer-encoding" not in req.headers
    (event,) = rec.requests
    assert event.fields["operation"] == "files.upload"
    assert event.fields["sobject"] == "ContentVersion"
    assert event.fields["outcome"] == "ok"
    assert event.fields["bytes_out"] == len(req.content)


def test_upload_path_defaults_filename_and_links(h: Harness, mock: respx.MockRouter, tmp_path: Path) -> None:
    route = mock.post(CV_URL).mock(return_value=created())
    path = tmp_path / "Q1 report.csv"
    path.write_bytes(b"a,b\n1,2\n")
    sf = h.client()
    assert h.run(sf.files.upload(path, link_to=ACCOUNT_ID)) == CV_ID
    entity, headers, content = parts(route.calls.last.request)
    # Only the base name reaches Salesforce, never the local directory.
    assert entity == {
        "PathOnClient": "Q1 report.csv",
        "Title": "Q1 report.csv",
        "FirstPublishLocationId": ACCOUNT_ID,
    }
    assert 'filename="Q1 report.csv"' in headers["Content-Disposition"]
    assert content == b"a,b\n1,2\n"
    assert h.run(sf.files.upload(str(path))) == CV_ID  # str paths work too


def test_upload_file_object_and_quoting(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(CV_URL).mock(return_value=created())
    sf = h.client()
    f = io.BytesIO(b"xxHELLO")
    f.seek(2)  # upload from the current position
    assert h.run(sf.files.upload(f, filename='dir/we"ird.txt')) == CV_ID
    entity, headers, content = parts(route.calls.last.request)
    assert content == b"HELLO"
    assert entity["PathOnClient"] == 'we"ird.txt'
    assert headers["Content-Disposition"].endswith('filename="we\\"ird.txt"')


def test_upload_stream_uses_chunked_encoding(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(CV_URL).mock(return_value=created())
    sf = h.client()
    assert h.run(sf.files.upload(stream_of(h, [b"ab", bytearray(b"cd")]), filename="s.bin")) == CV_ID
    req = route.calls.last.request
    assert parts(req)[2] == b"abcd"
    assert req.headers["transfer-encoding"] == "chunked"
    assert "content-length" not in req.headers


def test_upload_validates_before_sending(h: Harness, mock: respx.MockRouter, tmp_path: Path) -> None:
    sf = h.client()
    with pytest.raises(ValueError, match="filename"):
        h.run(sf.files.upload(b"x"))
    with pytest.raises(ValueError, match="link_to"):
        h.run(sf.files.upload(b"x", filename="a.txt", share_type="V"))
    with pytest.raises(ValueError, match="share_type"):
        h.run(sf.files.upload(b"x", filename="a.txt", link_to=ACCOUNT_ID, share_type="X"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="visibility"):
        h.run(sf.files.upload(b"x", filename="a.txt", link_to=ACCOUNT_ID, visibility="Everyone"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid Salesforce id"):
        h.run(sf.files.upload(b"x", filename="a.txt", link_to="../oauth2"))
    with pytest.raises(ValueError, match="control"):
        h.run(sf.files.upload(b"x", filename="a\r\nb.txt"))
    with pytest.raises(TypeError, match="upload\\(\\) takes"):
        h.run(sf.files.upload(12345, filename="a.txt"))  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        h.run(sf.files.upload(tmp_path / "missing.pdf"))
    assert not mock.calls


def test_upload_rejects_text_files_and_non_bytes_chunks(
    h: Harness, mock: respx.MockRouter, tmp_path: Path
) -> None:
    mock.post(CV_URL).mock(return_value=created())
    sf = h.client()
    path = tmp_path / "a.txt"
    path.write_text("hello")
    with path.open("r") as f, pytest.raises(TypeError, match="binary mode"):
        h.run(sf.files.upload(f))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must yield bytes"):
        h.run(sf.files.upload(stream_of(h, ["text"]), filename="a.txt"))  # type: ignore[list-item]


def test_upload_with_share_type_creates_link(h: Harness, mock: respx.MockRouter) -> None:
    upload = mock.post(CV_URL).mock(return_value=created())
    fields = mock.get(f"{CV_URL}/{CV_ID}").respond(json=load_fixture("files/version_document_id.json"))
    link = mock.post(LINK_URL).mock(return_value=created("files/link_created.json"))
    sf = h.client()
    cv = h.run(
        sf.files.upload(
            b"x", filename="a.txt", link_to=ACCOUNT_ID, share_type="C", visibility="InternalUsers"
        )
    )
    assert cv == CV_ID
    entity, _, _ = parts(upload.calls.last.request)
    assert "FirstPublishLocationId" not in entity
    assert fields.calls.last.request.url.params["fields"] == "ContentDocumentId"
    assert json.loads(link.calls.last.request.content) == {
        "ContentDocumentId": DOC_ID,
        "LinkedEntityId": ACCOUNT_ID,
        "ShareType": "C",
        "Visibility": "InternalUsers",
    }


def test_upload_link_failure_names_the_uploaded_version(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(CV_URL).mock(return_value=created())
    mock.get(f"{CV_URL}/{CV_ID}").respond(json=load_fixture("files/version_document_id.json"))
    mock.post(LINK_URL).respond(
        400, json=[{"errorCode": "INSUFFICIENT_ACCESS_OR_READONLY", "message": "insufficient access"}]
    )
    sf = h.client()
    with pytest.raises(esf.Forbidden) as ei:
        h.run(sf.files.upload(b"x", filename="a.txt", link_to=ACCOUNT_ID, visibility="AllUsers"))
    assert CV_ID in str(ei.value)
    assert "not linked" in ei.value.message


def test_upload_error_is_typed(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(CV_URL).respond(
        400, json=[{"errorCode": "STORAGE_LIMIT_EXCEEDED", "message": "storage limit exceeded"}]
    )
    sf = h.client()
    with pytest.raises(esf.StorageLimitExceeded) as ei:
        h.run(sf.files.upload(b"x", filename="a.txt"))
    assert (ei.value.operation, ei.value.sobject) == ("files.upload", "ContentVersion")


def test_upload_without_id_is_unexpected(h: Harness, mock: respx.MockRouter) -> None:
    mock.post(CV_URL).respond(201, json={"success": True})
    sf = h.client()
    with pytest.raises(esf.UnexpectedResponse, match="no id"):
        h.run(sf.files.upload(b"x", filename="a.txt"))


def test_upload_resent_from_path_after_connect_error(
    h: Harness, mock: respx.MockRouter, tmp_path: Path
) -> None:
    """A POST is resent only when it provably never left the client (ADR 0001)."""
    route = mock.post(CV_URL)
    route.side_effect = [httpx.ConnectError("refused"), created()]
    path = tmp_path / "a.bin"
    path.write_bytes(b"0123456789")
    sf = h.client()
    assert h.run(sf.files.upload(path)) == CV_ID
    assert route.call_count == 2
    assert parts(route.calls.last.request)[2] == b"0123456789"


def test_upload_seekable_file_resent_after_token_refresh(h: Harness, mock: respx.MockRouter) -> None:
    from ..conftest import TOKEN_URL, token_body

    mock.post(TOKEN_URL).respond(json=token_body("AT-fresh-0002"))
    route = mock.post(CV_URL)
    route.side_effect = [
        httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}]),
        created(),
    ]
    sf = h.client()
    assert h.run(sf.files.upload(io.BytesIO(b"payload"), filename="p.bin")) == CV_ID
    assert route.call_count == 2
    assert parts(route.calls.last.request)[2] == b"payload"


@pytest.mark.parametrize(
    "first",
    [
        httpx.ConnectError("refused"),
        httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}]),
    ],
)
def test_upload_stream_is_never_resent(h: Harness, mock: respx.MockRouter, first: Any) -> None:
    route = mock.post(CV_URL)
    route.side_effect = [first, created()]
    sf = h.client()
    with pytest.raises((esf.ConnectError, esf.SessionExpired)):
        h.run(sf.files.upload(stream_of(h, [b"once"]), filename="o.bin"))
    assert route.call_count == 1


# --- uploads never buffer the whole file ------------------------------------------------------


class _Consumer:
    """An httpx transport that reads the request body chunk by chunk, like a real socket.

    It records, for every chunk it receives, how far the producer had run ahead.
    """

    def __init__(self, produced: list[int] | None = None) -> None:
        self.produced = produced
        self.received = 0
        self.max_ahead = 0
        self.max_chunk = 0
        self.total = 0

    def _see(self, chunk: bytes) -> None:
        self.received += 1
        self.total += len(chunk)
        self.max_chunk = max(self.max_chunk, len(chunk))
        if self.produced is not None:
            self.max_ahead = max(self.max_ahead, self.produced[0] - self.received)

    def _response(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=load_fixture("files/version_created.json"), request=request)


class _AsyncConsumer(_Consumer, httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)
        async for chunk in request.stream:
            self._see(chunk)
        return self._response(request)


class _SyncConsumer(_Consumer, httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.SyncByteStream)
        for chunk in request.stream:
            self._see(chunk)
        return self._response(request)


def _consumer_client(h: Harness, consumer: _Consumer) -> Any:
    http: Any = httpx.AsyncClient(transport=consumer) if h.is_async else httpx.Client(transport=consumer)  # type: ignore[arg-type]
    return h.client(http_client=http)


def test_upload_stream_is_pulled_lazily(h: Harness) -> None:
    """The client reads the next chunk only after the network took the previous one."""
    produced = [0]
    n = 64
    chunks = [b"z" * CHUNK] * n

    def counting() -> Any:
        if h.is_async:

            async def agen() -> AsyncIterator[bytes]:
                for c in chunks:
                    produced[0] += 1
                    yield c

            return agen()

        def gen() -> Iterator[bytes]:
            for c in chunks:
                produced[0] += 1
                yield c

        return gen()

    consumer = (_AsyncConsumer if h.is_async else _SyncConsumer)(produced)
    sf = _consumer_client(h, consumer)
    assert h.run(sf.files.upload(counting(), filename="big.bin")) == CV_ID
    assert produced[0] == n
    assert consumer.max_ahead <= 1  # never more than the chunk in flight
    assert consumer.max_chunk <= CHUNK + 1024  # the multipart head is the only extra


def test_upload_from_path_keeps_memory_flat(h: Harness, tmp_path: Path) -> None:
    """A 32 MiB upload from disk allocates about one chunk, not the file size."""
    size = 32 * 1024 * 1024
    path = tmp_path / "big.bin"
    with path.open("wb") as f:
        for _ in range(size // CHUNK):
            f.write(b"\x00" * CHUNK)
    consumer = (_AsyncConsumer if h.is_async else _SyncConsumer)()
    sf = _consumer_client(h, consumer)
    tracemalloc.start()
    try:
        assert h.run(sf.files.upload(path)) == CV_ID
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert consumer.total > size
    assert peak < 4 * 1024 * 1024, f"peak {peak} bytes for a {size}-byte file"


# --- download ---------------------------------------------------------------------------------


def _body(h: Harness, pieces: list[bytes], error: Exception | None = None) -> Any:
    """A streamed response body (an async iterator for the async client), optionally failing."""
    if h.is_async:

        async def agen() -> AsyncIterator[bytes]:
            for p in pieces:
                yield p
            if error is not None:
                raise error

        return agen()

    def gen() -> Iterator[bytes]:
        yield from pieces
        if error is not None:
            raise error

    return gen()


def _chunks_response(h: Harness, data: bytes, **headers: str) -> httpx.Response:
    pieces = [data[i : i + 1000] for i in range(0, len(data), 1000)]
    return httpx.Response(
        200, headers={"content-type": "application/pdf", **headers}, content=_body(h, pieces)
    )


def _read_all(h: Harness, sf: Any, cv_id: str = CV_ID) -> tuple[bytes, Any]:
    if h.is_async:

        async def go() -> tuple[bytes, Any]:
            out = bytearray()
            async with sf.files.download(cv_id) as stream:
                async for chunk in stream:
                    out += chunk
            return bytes(out), stream

        return h.run(go())
    out = bytearray()
    with sf.files.download(cv_id) as stream:
        for chunk in stream:
            out += chunk
    return bytes(out), stream


def test_download_streams_and_logs_on_close(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    data = bytes(range(256)) * 40
    route = mock.get(f"{CV_URL}/{CV_ID}/VersionData").mock(
        return_value=_chunks_response(h, data, **{"content-length": str(len(data))})
    )
    sf = h.client(recorder=rec)
    got, stream = _read_all(h, sf)
    assert got == data
    assert stream.content_type == "application/pdf"
    assert stream.content_length == len(data)
    assert "application/pdf" in repr(stream)
    assert route.calls.last.request.headers["accept"] == "*/*"
    (event,) = rec.requests
    assert event.fields["operation"] == "files.download"
    assert event.fields["outcome"] == "ok"
    assert event.fields["bytes_in"] == len(data)
    assert event.fields["path_template"] == "/sobjects/ContentVersion/:id/VersionData"


def test_download_stop_early_closes(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{CV_URL}/{CV_ID}/VersionData").mock(return_value=_chunks_response(h, b"x" * 10_000))
    sf = h.client(recorder=rec)
    if h.is_async:

        async def go() -> Any:
            async with sf.files.download(CV_ID) as stream:
                async for _ in stream:
                    break
            await stream.aclose()  # a second close is a no-op
            return stream

        stream = h.run(go())
    else:
        with sf.files.download(CV_ID) as stream:
            for _ in stream:
                break
        stream.close()
    assert [e.fields["outcome"] for e in rec.requests] == ["ok"]
    with pytest.raises(RuntimeError, match="closed"):
        h.collect(stream)


def test_download_not_found_is_typed(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    mock.get(f"{CV_URL}/{CV_ID}/VersionData").respond(
        404, json=[{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]
    )
    sf = h.client(recorder=rec)
    with pytest.raises(esf.NotFound) as ei:
        _read_all(h, sf)
    assert ei.value.operation == "files.download"
    assert [e.fields["outcome"] for e in rec.requests] == ["error"]


def test_download_retries_5xx_before_streaming(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{CV_URL}/{CV_ID}/VersionData")
    route.side_effect = [httpx.Response(503, text="busy"), _chunks_response(h, b"ok")]
    sf = h.client()
    assert _read_all(h, sf)[0] == b"ok"
    assert route.call_count == 2


def test_download_error_mid_stream_is_typed(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    route = mock.get(f"{CV_URL}/{CV_ID}/VersionData")
    route.side_effect = [
        httpx.Response(200, content=_body(h, [b"partial"], httpx.ReadError("connection reset"))),
        httpx.Response(200, content=_body(h, [b"partial"], httpx.ReadTimeout("slow"))),
    ]
    sf = h.client(recorder=rec)
    with pytest.raises(esf.TransportError, match="reading the file"):
        _read_all(h, sf)
    with pytest.raises(esf.RequestTimeout):
        _read_all(h, sf)
    assert [e.fields["outcome"] for e in rec.requests] == ["error", "error"]
    assert [e.fields["error_class"] for e in rec.requests] == ["TransportError", "RequestTimeout"]


def test_download_user_error_inside_block(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    """An app error inside the block closes the stream; the transfer itself did not fail."""
    mock.get(f"{CV_URL}/{CV_ID}/VersionData").mock(return_value=_chunks_response(h, b"x" * 5000))
    sf = h.client(recorder=rec)

    class Boom(Exception):
        pass

    if h.is_async:

        async def go() -> None:
            async with sf.files.download(CV_ID) as stream:
                async for _ in stream:
                    raise Boom

        with pytest.raises(Boom):
            h.run(go())
    else:

        def go_sync() -> None:
            with sf.files.download(CV_ID) as stream:
                for _ in stream:
                    raise Boom

        with pytest.raises(Boom):
            go_sync()
    (event,) = rec.requests
    assert event.fields["outcome"] == "ok"


def test_download_bytes_and_attachment(h: Harness, mock: respx.MockRouter, rec: Recorder) -> None:
    v = mock.get(f"{CV_URL}/{CV_ID}/VersionData").respond(
        200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"}
    )
    a = mock.get(f"{DATA}/sobjects/Attachment/{ATT_ID}/Body").respond(
        200, content=b"%PDF", headers={"content-type": "application/pdf"}
    )
    sf = h.client(recorder=rec)
    assert h.run(sf.files.download_bytes(CV_ID)) == b"\x89PNG\r\n"
    assert h.run(sf.files.download_attachment(ATT_ID)) == b"%PDF"
    assert v.calls.last.request.headers["accept"] == "*/*"
    assert a.called
    assert [e.fields["operation"] for e in rec.requests] == [
        "files.download_bytes",
        "files.download_attachment",
    ]
    with pytest.raises(ValueError):
        h.run(sf.files.download_bytes("../x"))


# --- link and list_for ------------------------------------------------------------------------


def test_link(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(LINK_URL).mock(return_value=created("files/link_created.json"))
    sf = h.client()
    assert h.run(sf.files.link(DOC_ID, ACCOUNT_ID)) == LINK_ID
    assert json.loads(route.calls.last.request.content) == {
        "ContentDocumentId": DOC_ID,
        "LinkedEntityId": ACCOUNT_ID,
        "ShareType": "V",
    }
    h.run(sf.files.link(DOC_ID, ACCOUNT_ID, share_type="I", visibility="SharedUsers"))
    body = json.loads(route.calls.last.request.content)
    assert (body["ShareType"], body["Visibility"]) == ("I", "SharedUsers")
    with pytest.raises(ValueError):
        h.run(sf.files.link(DOC_ID, ACCOUNT_ID, share_type="X"))  # type: ignore[arg-type]


def test_list_for(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.get(f"{DATA}/query").respond(json=load_fixture("files/list_for.json"))
    sf = h.client()
    links = h.run(sf.files.list_for(ACCOUNT_ID))
    q = route.calls.last.request.url.params["q"]
    assert q.startswith("SELECT Id, ContentDocumentId, ShareType, Visibility, ContentDocument.Title")
    assert q.endswith(f"FROM ContentDocumentLink WHERE LinkedEntityId = '{ACCOUNT_ID}'")
    assert links == [
        esf.FileLink(
            link_id=LINK_ID,
            content_document_id=DOC_ID,
            latest_version_id=CV_ID,
            title="Invoice",
            file_extension="pdf",
            size=48213,
            share_type="V",
            visibility="AllUsers",
        ),
        esf.FileLink(
            link_id="06Axx0000000002AAA",
            content_document_id="069xx0000000002AAA",
            latest_version_id=None,
            title=None,
            file_extension=None,
            size=None,
            share_type="I",
            visibility="InternalUsers",
        ),
    ]
    with pytest.raises(ValueError):
        h.run(sf.files.list_for("' OR Id != '"))


def test_children_have_files(h: Harness, mock: respx.MockRouter) -> None:
    route = mock.post(LINK_URL).mock(return_value=created("files/link_created.json"))
    sf = h.client()
    child = sf.tagged("billing")
    assert h.run(child.files.link(DOC_ID, ACCOUNT_ID)) == LINK_ID
    assert route.called
