"""Files (§9.9): multipart envelopes for ContentVersion uploads and file-link parsing. No I/O.

Salesforce facts used here, from the *REST API Developer Guide*:

- *Insert or Update Blob Data*
  (https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/dome_sobject_insert_update_blob.htm):
  a multipart ``POST sobjects/ContentVersion``. The first part (``name="entity_content"``,
  ``application/json``) holds the field values. The second part (``name="VersionData"``) holds
  the bytes and needs a ``filename``. Every part needs ``Content-Disposition: form-data`` with a
  name and a ``Content-Type``. Multipart uploads take up to 2 GB per ContentVersion.
- *ContentDocumentLink* (Object Reference,
  https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_contentdocumentlink.htm):
  ``ShareType`` is ``V`` (viewer), ``C`` (collaborator) or ``I`` (inferred); ``Visibility`` is
  ``AllUsers``, ``InternalUsers`` or ``SharedUsers``. A query must filter on ``Id``,
  ``ContentDocumentId`` or ``LinkedEntityId``.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from typing import Any, Literal, cast

from ..errors import UnexpectedResponse
from ..models import FileLink
from .http import check_record_id

__all__ = [
    "CHUNK_SIZE",
    "LIST_FOR_SOQL",
    "SHARE_TYPES",
    "VISIBILITIES",
    "ShareType",
    "Visibility",
    "check_share_type",
    "check_visibility",
    "multipart_envelope",
    "new_boundary",
    "parse_created_id",
    "parse_file_link",
    "safe_filename",
    "version_entity",
]

#: Bytes read from a file (or the network) per chunk. Uploads hold at most about one chunk.
CHUNK_SIZE = 256 * 1024

ShareType = Literal["V", "C", "I"]
Visibility = Literal["AllUsers", "InternalUsers", "SharedUsers"]
SHARE_TYPES: frozenset[str] = frozenset({"V", "C", "I"})
VISIBILITIES: frozenset[str] = frozenset({"AllUsers", "InternalUsers", "SharedUsers"})

LIST_FOR_SOQL = (
    "SELECT Id, ContentDocumentId, ShareType, Visibility, ContentDocument.Title, "
    "ContentDocument.FileExtension, ContentDocument.ContentSize, "
    "ContentDocument.LatestPublishedVersionId FROM ContentDocumentLink WHERE LinkedEntityId = {}"
)


def check_share_type(value: str) -> str:
    """Validate a ``ContentDocumentLink.ShareType`` value (``V``, ``C`` or ``I``)."""
    if value not in SHARE_TYPES:
        raise ValueError(f"share_type must be 'V', 'C' or 'I', got {value!r}")
    return value


def check_visibility(value: str) -> str:
    """Validate a ``ContentDocumentLink.Visibility`` value."""
    if value not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {sorted(VISIBILITIES)}, got {value!r}")
    return value


def safe_filename(name: str) -> str:
    """The base name of ``name``, safe for ``PathOnClient`` and a quoted multipart header.

    Directories are dropped (``/tmp/a.pdf`` → ``a.pdf``), so local paths never reach the org.

    Raises:
        ValueError: empty, or contains control characters (which would break the header).
    """
    base = os.path.basename(str(name).replace("\\", "/"))
    if not base or base in (".", ".."):
        raise ValueError(f"filename must name a file, got {name!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in base):
        raise ValueError("filename must not contain control characters")
    return base


def version_entity(filename: str, title: str | None, first_publish_location: str | None) -> dict[str, Any]:
    """Field values for the ``entity_content`` part of a ContentVersion upload."""
    entity: dict[str, Any] = {"PathOnClient": filename, "Title": title if title is not None else filename}
    if first_publish_location is not None:
        entity["FirstPublishLocationId"] = check_record_id(first_publish_location)
    return entity


def new_boundary() -> str:
    """A random multipart boundary that can't occur by chance in the file."""
    return f"esf-{secrets.token_hex(16)}"


def multipart_envelope(entity_json: bytes, filename: str, boundary: str) -> tuple[bytes, bytes, str]:
    """Bytes around the file content of a ContentVersion upload.

    The request body is ``head + <file bytes> + tail``, so the file itself is streamed and
    never copied into one buffer.

    Returns:
        ``(head, tail, content_type)``.
    """
    quoted = filename.replace("\\", "\\\\").replace('"', '\\"')
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="entity_content"\r\n'
        "Content-Type: application/json\r\n\r\n"
    ).encode() + entity_json
    head += (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="VersionData"; filename="{quoted}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head, tail, f'multipart/form-data; boundary="{boundary}"'


def parse_created_id(body: Any, operation: str) -> str:
    """The ``id`` of a ``{"id": ..., "success": true}`` create response."""
    if not isinstance(body, dict) or not body.get("id"):  # pyright: ignore[reportUnknownMemberType]
        raise UnexpectedResponse("create returned no id", operation=operation)
    return str(body["id"])  # pyright: ignore[reportUnknownArgumentType]


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_file_link(row: Mapping[str, Any]) -> FileLink:
    """Turn one row of :data:`LIST_FOR_SOQL` into a :class:`FileLink`."""
    doc_any: Any = row.get("ContentDocument")
    doc: Mapping[str, Any] = cast("Mapping[str, Any]", doc_any) if isinstance(doc_any, Mapping) else {}
    link_id, doc_id = row.get("Id"), row.get("ContentDocumentId")
    if not link_id or not doc_id:
        raise UnexpectedResponse("ContentDocumentLink row without Id or ContentDocumentId")

    def text(value: Any) -> str | None:
        return str(value) if value is not None else None

    return FileLink(
        link_id=str(link_id),
        content_document_id=str(doc_id),
        latest_version_id=text(doc.get("LatestPublishedVersionId")),
        title=text(doc.get("Title")),
        file_extension=text(doc.get("FileExtension")),
        size=_int_or_none(doc.get("ContentSize")),
        share_type=text(row.get("ShareType")),
        visibility=text(row.get("Visibility")),
    )
