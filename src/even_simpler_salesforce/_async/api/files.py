"""Files: ``sf.files`` (§9.9). ContentVersion uploads and downloads, streamed both ways."""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any

from ...core.files import (
    CHUNK_SIZE,
    LIST_FOR_SOQL,
    ShareType,
    Visibility,
    check_share_type,
    check_visibility,
    multipart_envelope,
    new_boundary,
    parse_created_id,
    parse_file_link,
    safe_filename,
    version_entity,
)
from ...core.http import RequestSpec, check_record_id
from ...errors import SalesforceError
from ...models import FileLink
from .. import _compat

if TYPE_CHECKING:
    from ..client import AsyncSalesforce
    from ..transport import AsyncFileStream

__all__ = ["AsyncFiles"]

#: Downloads accept any content type: the body is the file, not JSON.
_ANY = {"Accept": "*/*"}


@dataclass(frozen=True, slots=True)
class _Source:
    chunks: Callable[[], AsyncIterator[bytes]]  # a fresh iterator per attempt
    length: int | None  # None: unknown, sent with chunked transfer encoding
    once: bool  # can be read only once, so the request is never resent
    name: str | None  # a file name to use when the caller passes none


def _bytes_source(data: bytes | bytearray | memoryview) -> _Source:
    view = memoryview(data)

    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(view), CHUNK_SIZE):
            yield bytes(view[i : i + CHUNK_SIZE])

    return _Source(chunks, len(view), False, None)


def _path_source(path: str | os.PathLike[str]) -> _Source:
    fspath = os.fspath(path)
    size = os.stat(fspath).st_size  # raises FileNotFoundError before anything is sent

    async def chunks() -> AsyncIterator[bytes]:
        f = await _compat.to_thread(open, fspath, "rb")
        try:
            while chunk := await _compat.to_thread(f.read, CHUNK_SIZE):
                yield chunk
        finally:
            f.close()

    return _Source(chunks, size, False, os.path.basename(fspath))


def _file_source(f: IO[bytes]) -> _Source:
    raw_name: Any = getattr(f, "name", None)
    name = os.path.basename(raw_name) if isinstance(raw_name, str) else None
    seekable = bool(getattr(f, "seekable", lambda: False)())
    start = f.tell() if seekable else 0
    length: int | None = None
    if seekable:
        length = f.seek(0, os.SEEK_END) - start
        f.seek(start)

    async def chunks() -> AsyncIterator[bytes]:
        if seekable:
            f.seek(start)  # a resend starts from the same place
        while chunk := await _compat.to_thread(f.read, CHUNK_SIZE):
            if not isinstance(chunk, bytes):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError("open the file in binary mode ('rb')")
            yield chunk

    return _Source(chunks, length, not seekable, name)


def _stream_source(stream: AsyncIterable[bytes]) -> _Source:
    used = [False]

    async def chunks() -> AsyncIterator[bytes]:
        if used[0]:
            raise RuntimeError("the upload stream was already read")
        used[0] = True
        async for chunk in stream:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError(f"upload streams must yield bytes, got {type(chunk).__name__}")
            yield bytes(chunk)

    return _Source(chunks, None, True, None)


def _source(source: Any) -> _Source:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return _bytes_source(source)  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(source, (str, os.PathLike)):
        return _path_source(source)  # pyright: ignore[reportUnknownArgumentType]
    if hasattr(source, "read"):
        return _file_source(source)
    if hasattr(source, "__aiter__"):
        return _stream_source(source)
    raise TypeError(
        "upload() takes bytes, a path, a binary file object or an iterator of bytes, "
        f"got {type(source).__name__}"
    )


class AsyncFiles:
    """Salesforce Files: upload, download and share ``ContentVersion`` files.

    Uploads and downloads are streamed. The client never holds a whole file in memory, so files
    up to the 2 GB ContentVersion limit work.
    """

    def __init__(self, client: AsyncSalesforce) -> None:
        self._sf = client

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
        """Upload a file as a new ``ContentVersion``. Returns the ContentVersion id.

        The client sends a multipart ``POST sobjects/ContentVersion`` and streams the file
        content, so memory use stays at about one chunk whatever the file size.

        Args:
            source: The file content: ``bytes``, a path, a binary file object, or an iterator
                of ``bytes`` chunks. A path or a seekable file is read again if the request is
                resent. An iterator can be read only once, so the client never resends it.
            filename: File name stored as ``PathOnClient``; Salesforce takes the file type
                from its extension. Defaults to the name of the path or file object. Required
                for ``bytes`` and iterators.
            title: Title shown in Salesforce. Defaults to ``filename``.
            link_to: Record id to share the file with. Without ``share_type`` and
                ``visibility`` the client sets ``FirstPublishLocationId``, and Salesforce links
                the file in the same request.
            share_type: ``"V"`` (viewer), ``"C"`` (collaborator) or ``"I"`` (inferred) for the
                link to ``link_to``. Defaults to ``"V"`` if only ``visibility`` is given.
            visibility: ``"AllUsers"``, ``"InternalUsers"`` or ``"SharedUsers"`` for the link.

        Raises:
            ValueError: no file name, an invalid id, or ``share_type``/``visibility`` without
                ``link_to``.
            SalesforceError: Salesforce rejected the upload or the link. If only the link
                failed, the message names the uploaded ContentVersion id.

        Example:
            cv_id = await sf.files.upload("invoice.pdf", link_to=account_id)
        """
        if link_to is None and (share_type is not None or visibility is not None):
            raise ValueError("share_type and visibility need link_to")
        if link_to is not None:
            check_record_id(link_to)
        if share_type is not None:
            check_share_type(share_type)
        if visibility is not None:
            check_visibility(visibility)
        src = _source(source)
        name = filename if filename is not None else src.name
        if name is None:
            raise ValueError("pass filename= (Salesforce needs it for the file type)")
        name = safe_filename(name)
        explicit_link = link_to is not None and (share_type is not None or visibility is not None)
        entity = version_entity(name, title, None if explicit_link else link_to)
        entity_json, _ = self._sf._transport.encode(entity)
        head, tail, ctype = multipart_envelope(entity_json, name, new_boundary())

        async def body() -> AsyncIterator[bytes]:
            yield head
            async for chunk in src.chunks():
                yield chunk
            yield tail

        raw = await self._sf._send(
            RequestSpec(
                "POST",
                "sobjects/ContentVersion",
                content_type=ctype,
                body_stream=body,
                body_length=None if src.length is None else len(head) + src.length + len(tail),
                body_once=src.once,
                operation="files.upload",
                sobject="ContentVersion",
                records=1,
                dml=True,
            )
        )
        cv_id = parse_created_id(raw.json(), "files.upload")
        if explicit_link:
            assert link_to is not None
            try:
                version = await self._sf.sobject("ContentVersion").get(cv_id, fields=["ContentDocumentId"])
                await self.link(
                    str(version["ContentDocumentId"]),
                    link_to,
                    share_type=share_type or "V",
                    visibility=visibility,
                )
            except SalesforceError as exc:
                exc.message = (
                    f"{exc.message} (uploaded as ContentVersion {cv_id}, but not linked to {link_to})"
                )
                exc.args = (exc.message,)
                raise
        return cv_id

    @contextlib.asynccontextmanager
    async def download(self, content_version_id: str) -> AsyncIterator[AsyncFileStream]:
        """Stream the content of a ``ContentVersion``: ``GET sobjects/ContentVersion/{id}/VersionData``.

        Use it as a context manager and iterate over the stream for ``bytes`` chunks. The
        connection closes when the block ends, also if you stop reading early.

        Args:
            content_version_id: Id of the ContentVersion (``068...``).

        Raises:
            NotFound: no such ContentVersion, or no access to it.
            TransportError: the connection failed while you read the stream.

        Example:
            async with sf.files.download(cv_id) as stream:
                async for chunk in stream:
                    out.write(chunk)
        """
        raw = await self._sf._send(
            RequestSpec(
                "GET",
                f"sobjects/ContentVersion/{check_record_id(content_version_id)}/VersionData",
                headers=_ANY,
                operation="files.download",
                sobject="ContentVersion",
                expect_json=False,
                stream_response=True,
            )
        )
        stream: AsyncFileStream = raw.stream
        try:
            yield stream
        finally:
            await stream.aclose()

    async def download_bytes(self, content_version_id: str) -> bytes:
        """The whole content of a ``ContentVersion`` as ``bytes``.

        This reads the file into memory. Use :meth:`download` for large files.
        """
        return await self._blob("ContentVersion", content_version_id, "VersionData", "files.download_bytes")

    async def download_attachment(self, attachment_id: str) -> bytes:
        """The content of a legacy ``Attachment`` (``GET sobjects/Attachment/{id}/Body``) as ``bytes``."""
        return await self._blob("Attachment", attachment_id, "Body", "files.download_attachment")

    async def link(
        self,
        content_document_id: str,
        record_id: str,
        *,
        share_type: ShareType = "V",
        visibility: Visibility | None = None,
    ) -> str:
        """Share a file with a record: create a ``ContentDocumentLink``. Returns the link id.

        Args:
            content_document_id: Id of the ContentDocument (``069...``), not of a version.
            record_id: Record, user, group or library to link the file to.
            share_type: ``"V"`` (viewer), ``"C"`` (collaborator) or ``"I"`` (inferred).
            visibility: ``"AllUsers"``, ``"InternalUsers"`` or ``"SharedUsers"``. ``None``
                lets Salesforce choose.
        """
        data: dict[str, Any] = {
            "ContentDocumentId": check_record_id(content_document_id),
            "LinkedEntityId": check_record_id(record_id),
            "ShareType": check_share_type(share_type),
        }
        if visibility is not None:
            data["Visibility"] = check_visibility(visibility)
        content, ctype = self._sf._transport.encode(data)
        raw = await self._sf._send(
            RequestSpec(
                "POST",
                "sobjects/ContentDocumentLink",
                content=content,
                content_type=ctype,
                operation="files.link",
                sobject="ContentDocumentLink",
                records=1,
                dml=True,
            )
        )
        return parse_created_id(raw.json(), "files.link")

    async def list_for(self, record_id: str) -> list[FileLink]:
        """The files linked to a record, one :class:`FileLink` per ``ContentDocumentLink``.

        Runs one SOQL query on ``ContentDocumentLink`` (plus one call per further page).
        """
        return [
            parse_file_link(row)
            async for row in self._sf.query_iter(LIST_FOR_SOQL, check_record_id(record_id))
        ]

    async def _blob(self, sobject: str, record_id: str, field: str, operation: str) -> bytes:
        raw = await self._sf._send(
            RequestSpec(
                "GET",
                f"sobjects/{sobject}/{check_record_id(record_id)}/{field}",
                headers=_ANY,
                operation=operation,
                sobject=sobject,
                expect_json=False,
            )
        )
        return raw.content
