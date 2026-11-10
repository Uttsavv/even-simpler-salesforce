"""``fake.files``: in-memory Salesforce Files for :class:`FakeSalesforce` (§9.9, §14).

Uploads become ``ContentDocument``, ``ContentVersion`` and ``ContentDocumentLink`` records in
the fake's store, so ``fake.records("ContentVersion")`` and ``fake.file_content(cv_id)`` show
what the code under test uploaded. The file bytes live beside the records, not in them.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from typing import IO, Any

from ..core.files import (
    CHUNK_SIZE,
    ShareType,
    Visibility,
    check_share_type,
    check_visibility,
    safe_filename,
)
from ..core.http import check_record_id
from ..errors import InvalidReference
from ..models import FileLink
from ._state import FakeState, Who

__all__ = ["AsyncFakeFileStream", "AsyncFakeFiles", "FakeFileStream", "FakeFiles"]


def _read_sync(source: Any) -> tuple[bytes, str | None]:
    """The whole content of an upload source, plus a default file name."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source), None  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
        with open(path, "rb") as f:  # pyright: ignore[reportUnknownArgumentType]
            return f.read(), os.path.basename(path)  # pyright: ignore[reportUnknownArgumentType]
    if hasattr(source, "read"):
        data: Any = source.read()
        if not isinstance(data, bytes):
            raise TypeError("open the file in binary mode ('rb')")
        name: Any = getattr(source, "name", None)
        return data, os.path.basename(name) if isinstance(name, str) else None
    if isinstance(source, Iterable):
        out = bytearray()
        for chunk in source:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError(f"upload streams must yield bytes, got {type(chunk).__name__}")  # pyright: ignore[reportUnknownArgumentType]
            out += chunk
        return bytes(out), None
    raise TypeError(
        "upload() takes bytes, a path, a binary file object or an iterator of bytes, "
        f"got {type(source).__name__}"
    )


def _extension(filename: str) -> str | None:
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    return ext or None


class FakeFileStream:
    """What ``fake.files.download()`` yields: iterate for ``bytes`` chunks."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._closed = False
        self.content_type: str | None = "application/octet-stream"
        self.content_length: int | None = len(data)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.content_length} bytes>"

    def __iter__(self) -> Iterator[bytes]:
        if self._closed:
            raise RuntimeError("the file stream is closed")
        for i in range(0, len(self._data), CHUNK_SIZE):
            yield self._data[i : i + CHUNK_SIZE]

    def close(self) -> None:
        """Close the stream. Safe to call twice."""
        self._closed = True


class AsyncFakeFileStream:
    """Async twin of :class:`FakeFileStream`."""

    def __init__(self, inner: FakeFileStream) -> None:
        self._inner = inner
        self.content_type = inner.content_type
        self.content_length = inner.content_length

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.content_length} bytes>"

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        """Close the stream. Safe to call twice."""
        self._inner.close()


class FakeFiles:
    """Fake of ``sf.files``. Same arguments, validation and operation names as the real one."""

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    # --- shared with the test-side controls ---------------------------------------------

    def _store_file(
        self,
        data: bytes,
        name: str,
        title: str | None,
        link_to: str | None,
        share_type: str | None,
        visibility: str | None,
    ) -> str:
        st = self._state
        doc_id = st.new_id("ContentDocument")
        cv_id = st.new_id("ContentVersion")
        title = title if title is not None else name
        common = {"Title": title, "FileExtension": _extension(name), "ContentSize": len(data)}
        st.seed("ContentDocument", {"Id": doc_id, "LatestPublishedVersionId": cv_id, **common})
        st.seed(
            "ContentVersion",
            {
                "Id": cv_id,
                "ContentDocumentId": doc_id,
                "PathOnClient": name,
                "IsLatest": True,
                "VersionNumber": "1",
                **({"FirstPublishLocationId": link_to} if link_to else {}),
                **common,
            },
        )
        st.blobs[cv_id[:15]] = data
        if link_to is not None:
            self._link(doc_id, link_to, share_type or "V", visibility or "AllUsers")
        return cv_id

    def _link(self, doc_id: str, record_id: str, share_type: str, visibility: str | None) -> str:
        st = self._state
        if st.snapshot_record("ContentDocument", doc_id) is None:
            raise InvalidReference(
                f"invalid cross reference id: {doc_id}",
                error_code="INVALID_CROSS_REFERENCE_KEY",
                fields=("ContentDocumentId",),
                status=400,
                operation="files.link",
                sobject="ContentDocumentLink",
            )
        return st.seed(
            "ContentDocumentLink",
            {
                "ContentDocumentId": doc_id,
                "LinkedEntityId": record_id,
                "ShareType": share_type,
                "Visibility": visibility or "AllUsers",
            },
        )

    def _content(self, cv_id: str, operation: str) -> bytes:
        self._state.find("ContentVersion", cv_id, operation)
        return self._state.blobs.get(cv_id[:15], b"")

    # --- the sf.files surface --------------------------------------------------------------

    def upload(
        self,
        source: bytes | str | os.PathLike[str] | IO[bytes] | Iterable[bytes],
        *,
        filename: str | None = None,
        title: str | None = None,
        link_to: str | None = None,
        share_type: ShareType | None = None,
        visibility: Visibility | None = None,
    ) -> str:
        """Store the file in memory. Returns the new ContentVersion id."""
        if link_to is None and (share_type is not None or visibility is not None):
            raise ValueError("share_type and visibility need link_to")
        if link_to is not None:
            check_record_id(link_to)
        if share_type is not None:
            check_share_type(share_type)
        if visibility is not None:
            check_visibility(visibility)
        data, default_name = _read_sync(source)
        name = filename if filename is not None else default_name
        if name is None:
            raise ValueError("pass filename= (Salesforce needs it for the file type)")
        name = safe_filename(name)
        return self._state.run(
            self._who,
            "files.upload",
            "ContentVersion",
            {
                "filename": name,
                "title": title,
                "link_to": link_to,
                "share_type": share_type,
                "visibility": visibility,
                "size": len(data),
            },
            lambda: self._store_file(data, name, title, link_to, share_type, visibility),
            method="POST",
            path="sobjects/ContentVersion",
            records=1,
        )

    @contextlib.contextmanager
    def download(self, content_version_id: str) -> Iterator[FakeFileStream]:
        """Yield a stream over the stored bytes of a ContentVersion."""
        data = self._state.run(
            self._who,
            "files.download",
            "ContentVersion",
            {"id": content_version_id},
            lambda: self._content(content_version_id, "files.download"),
            method="GET",
            path=f"sobjects/ContentVersion/{check_record_id(content_version_id)}/VersionData",
        )
        stream = FakeFileStream(data)
        try:
            yield stream
        finally:
            stream.close()

    def download_bytes(self, content_version_id: str) -> bytes:
        """The stored bytes of a ContentVersion."""
        return self._state.run(
            self._who,
            "files.download_bytes",
            "ContentVersion",
            {"id": content_version_id},
            lambda: self._content(content_version_id, "files.download_bytes"),
            method="GET",
            path=f"sobjects/ContentVersion/{check_record_id(content_version_id)}/VersionData",
        )

    def download_attachment(self, attachment_id: str) -> bytes:
        """The ``Body`` of an ``Attachment`` seeded with ``fake.add("Attachment", {"Body": b"..."})``."""

        def fn() -> bytes:
            body: Any = self._state.find("Attachment", attachment_id, "files.download_attachment").get("Body")
            if isinstance(body, str):
                return body.encode()
            return bytes(body or b"")

        return self._state.run(
            self._who,
            "files.download_attachment",
            "Attachment",
            {"id": attachment_id},
            fn,
            method="GET",
            path=f"sobjects/Attachment/{check_record_id(attachment_id)}/Body",
        )

    def link(
        self,
        content_document_id: str,
        record_id: str,
        *,
        share_type: ShareType = "V",
        visibility: Visibility | None = None,
    ) -> str:
        """Store a ``ContentDocumentLink``. Returns its id."""
        check_record_id(content_document_id)
        check_record_id(record_id)
        check_share_type(share_type)
        if visibility is not None:
            check_visibility(visibility)
        return self._state.run(
            self._who,
            "files.link",
            "ContentDocumentLink",
            {
                "content_document_id": content_document_id,
                "record_id": record_id,
                "share_type": share_type,
                "visibility": visibility,
            },
            lambda: self._link(content_document_id, record_id, share_type, visibility),
            method="POST",
            path="sobjects/ContentDocumentLink",
            records=1,
        )

    def list_for(self, record_id: str) -> list[FileLink]:
        """The stored links of a record, as :class:`FileLink` values."""
        check_record_id(record_id)

        def fn() -> list[FileLink]:
            out: list[FileLink] = []
            for link in self._state.snapshot_records("ContentDocumentLink"):
                if str(link.get("LinkedEntityId", ""))[:15] != record_id[:15]:
                    continue
                doc = self._state.snapshot_record("ContentDocument", link["ContentDocumentId"]) or {}
                out.append(
                    FileLink(
                        link_id=link["Id"],
                        content_document_id=link["ContentDocumentId"],
                        latest_version_id=doc.get("LatestPublishedVersionId"),
                        title=doc.get("Title"),
                        file_extension=doc.get("FileExtension"),
                        size=doc.get("ContentSize"),
                        share_type=link.get("ShareType"),
                        visibility=link.get("Visibility"),
                    )
                )
            return out

        return self._state.run(
            self._who,
            "files.list_for",
            "ContentDocumentLink",
            {"record_id": record_id},
            fn,
            method="GET",
            path="query",
        )


class AsyncFakeFiles:
    """Async twin of :class:`FakeFiles`."""

    def __init__(self, inner: FakeFiles) -> None:
        self._inner = inner

    async def upload(
        self,
        source: bytes | str | os.PathLike[str] | IO[bytes] | AsyncIterable[bytes],
        *,
        filename: str | None = None,
        title: str | None = None,
        link_to: str | None = None,
        share_type: ShareType | None = None,
        visibility: Visibility | None = None,
    ) -> str:
        """See :meth:`FakeFiles.upload`. Async iterators are read in full."""
        data: Any = source
        if isinstance(source, AsyncIterable):
            data = [chunk async for chunk in source]
        return self._inner.upload(
            data,
            filename=filename,
            title=title,
            link_to=link_to,
            share_type=share_type,
            visibility=visibility,
        )

    @contextlib.asynccontextmanager
    async def download(self, content_version_id: str) -> AsyncIterator[AsyncFakeFileStream]:
        """See :meth:`FakeFiles.download`."""
        with self._inner.download(content_version_id) as inner:
            stream = AsyncFakeFileStream(inner)
            yield stream

    async def download_bytes(self, content_version_id: str) -> bytes:
        """See :meth:`FakeFiles.download_bytes`."""
        return self._inner.download_bytes(content_version_id)

    async def download_attachment(self, attachment_id: str) -> bytes:
        """See :meth:`FakeFiles.download_attachment`."""
        return self._inner.download_attachment(attachment_id)

    async def link(
        self,
        content_document_id: str,
        record_id: str,
        *,
        share_type: ShareType = "V",
        visibility: Visibility | None = None,
    ) -> str:
        """See :meth:`FakeFiles.link`."""
        return self._inner.link(content_document_id, record_id, share_type=share_type, visibility=visibility)

    async def list_for(self, record_id: str) -> list[FileLink]:
        """See :meth:`FakeFiles.list_for`."""
        return self._inner.list_for(record_id)
