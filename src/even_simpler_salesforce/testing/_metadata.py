"""``fake.metadata``: an in-memory Metadata API for :class:`FakeSalesforce` (§9.15, §14).

Deployments finish at once: ``deploy()`` returns ``Pending`` like the real client and the
first ``deploy_status()`` returns the final result, ``Succeeded`` unless the test scripted
another outcome with ``fake.on_deploy(...)``. The deployed zip is kept, so
``fake.deployed_zip(job.id)`` shows what the code under test sent.

Retrieve, ``list_metadata`` and ``describe_metadata`` read components seeded with
``fake.add_metadata(type, full_name, files)``. A deployment does not add components: the fake
doesn't map zip folders to metadata types.
"""

from __future__ import annotations

import dataclasses
import io
import os
import zipfile
from collections.abc import Callable, Iterable, Mapping
from typing import IO, Any, TypeAlias
from xml.sax.saxutils import escape

from .._sync.api.metadata import (
    _ZIP_MAGIC,  # pyright: ignore[reportPrivateUsage]
    DEFAULT_TIMEOUT,
    _Clock,  # pyright: ignore[reportPrivateUsage]
    _extract,  # pyright: ignore[reportPrivateUsage]
    _prepare_source,  # pyright: ignore[reportPrivateUsage]
)
from ..core.http import check_record_id
from ..core.metadata import (
    NS_METADATA,
    list_metadata_envelope,
    metadata_soap_path,
    package_types,
    safe_member,
)
from ..errors import NotFound
from ..models import (
    DeployOptions,
    DeployResult,
    FileProperties,
    MetadataDescribe,
    MetadataType,
    RetrieveMessage,
    RetrieveResult,
)
from ._state import FakeState, Who

__all__ = ["AsyncFakeMetadata", "DeployOutcome", "FakeMetadata", "seed_component"]

#: What ``fake.on_deploy`` takes: a result (its ``id`` is replaced) or ``(zip, options) -> result``.
DeployOutcome: TypeAlias = "DeployResult | Callable[[bytes, DeployOptions], DeployResult]"


def seed_component(
    state: FakeState, metadata_type: str, full_name: str, files: Mapping[str, bytes | str]
) -> None:
    """Store one component for retrieve, ``list_metadata`` and ``describe_metadata``."""
    list_metadata_envelope(metadata_type, None, state.api_version, "")  # same type-name check
    if not full_name:
        raise ValueError("full_name must not be empty")
    if not files:
        raise ValueError("files must name at least one file, e.g. {'classes/Foo.cls': '...'}")
    clean: dict[str, bytes] = {}
    for path, content in files.items():
        name = safe_member(path)
        if name.endswith("/") or name == "package.xml":
            raise ValueError(f"not a component file: {path!r}")
        clean[name] = content.encode() if isinstance(content, str) else bytes(content)
    with state.lock:
        state.metadata_components[(metadata_type, full_name)] = clean


def _package_xml(types: Mapping[str, list[str]], api_version: str) -> bytes:
    parts = "".join(
        "<types>"
        + "".join(f"<members>{escape(m)}</members>" for m in members)
        + f"<name>{escape(name)}</name></types>"
        for name, members in types.items()
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Package xmlns="{NS_METADATA}">'
        f"{parts}<version>{escape(api_version)}</version></Package>"
    ).encode()


def _file_properties(metadata_type: str, full_name: str, files: Mapping[str, bytes]) -> FileProperties:
    main = next((f for f in files if not f.endswith("-meta.xml")), next(iter(files)))
    return FileProperties(
        type=metadata_type, full_name=full_name, file_name=main, manageable_state="unmanaged"
    )


def _count_components(data: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return sum(
            1
            for n in zf.namelist()
            if not n.endswith("/") and n.rsplit("/", 1)[-1] != "package.xml" and not n.endswith("-meta.xml")
        )


class FakeMetadata:
    """Fake of ``sf.metadata``. Same arguments, validation and operation names as the real one."""

    def __init__(self, state: FakeState, who: Who) -> None:
        self._state = state
        self._who = who

    # --- deploy -------------------------------------------------------------------------

    def _read_source(self, source: Any, options: DeployOptions) -> bytes:
        if isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)  # pyright: ignore[reportUnknownArgumentType]
            if data[:4] not in _ZIP_MAGIC:
                raise ValueError("source bytes are not a zip file")
            return data
        if isinstance(source, (str, os.PathLike)):
            f: IO[bytes] = _prepare_source(os.fspath(source), options.single_package)  # pyright: ignore[reportUnknownArgumentType]
            with f:
                return f.read()
        raise TypeError(f"deploy() takes zip bytes or a path, got {type(source).__name__}")

    def _outcome(self, deploy_id: str, data: bytes, options: DeployOptions) -> DeployResult:
        st = self._state
        outcome: DeployOutcome | None = None
        for i, (o, times) in enumerate(st.deploy_outcomes):
            outcome = o
            if times is not None:
                if times <= 1:
                    del st.deploy_outcomes[i]
                else:
                    st.deploy_outcomes[i] = (o, times - 1)
            break
        if outcome is None:
            try:
                total = _count_components(data)
            except zipfile.BadZipFile:
                total = 0
            return DeployResult(
                id=deploy_id,
                status="Succeeded",
                done=True,
                success=True,
                check_only=options.check_only,
                components_total=total,
                components_deployed=total,
            )
        result = outcome if isinstance(outcome, DeployResult) else outcome(data, options)
        return dataclasses.replace(result, id=deploy_id)

    def deploy(
        self, source: bytes | str | os.PathLike[str], *, options: DeployOptions | None = None
    ) -> DeployResult:
        """Keep the zip and return a ``Pending`` result, like ``sf.metadata.deploy``."""
        options = options if options is not None else DeployOptions()
        data = self._read_source(source, options)

        def fn() -> DeployResult:
            deploy_id = self._state.new_id("DeployRequest")
            final = self._outcome(deploy_id, data, options)
            self._state.deployments[deploy_id] = (data, options, final)
            return DeployResult(id=deploy_id, status="Pending", check_only=options.check_only)

        return self._state.run(
            self._who,
            "metadata.deploy",
            None,
            {"options": options, "size": len(data)},
            fn,
            method="POST",
            path="metadata/deployRequest",
        )

    def deploy_status(
        self, deploy_id: str, *, wait: bool = False, timeout: float = DEFAULT_TIMEOUT
    ) -> DeployResult:
        """The final result of a fake deployment (the fake finishes deployments at once)."""
        check_record_id(deploy_id)
        if wait:
            _Clock(timeout)  # same timeout check as the client

        def fn() -> DeployResult:
            for key, (_, _, result) in self._state.deployments.items():
                if key[:15] == deploy_id[:15]:
                    return result
            raise _not_found(deploy_id)

        return self._state.run(
            self._who,
            "metadata.deploy_status",
            None,
            {"id": deploy_id, "wait": wait},
            fn,
            method="GET",
            path=f"metadata/deployRequest/{deploy_id}",
        )

    # --- retrieve, list, describe -------------------------------------------------------

    def _retrieve(self, package: str | bytes | Mapping[str, Iterable[str]]) -> RetrieveResult:
        st = self._state
        types = package_types(package)
        listed: dict[str, list[str]] = {}
        files: dict[str, bytes] = {}
        props: list[FileProperties] = []
        messages: list[RetrieveMessage] = []
        for name, members in types:
            listed[name] = list(members)
            found: set[str] = set()
            for (mtype, full_name), comp in sorted(st.metadata_components.items()):
                if mtype == name and ("*" in members or full_name in members):
                    found.add(full_name)
                    files.update(comp)
                    props.append(_file_properties(mtype, full_name, comp))
            for m in members:
                if m != "*" and m not in found:
                    messages.append(
                        RetrieveMessage(
                            file_name="package.xml",
                            problem=f"Entity of type '{name}' named '{m}' cannot be found",
                        )
                    )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("package.xml", _package_xml(listed, st.api_version))
            for path in sorted(files):
                zf.writestr(path, files[path])
        return RetrieveResult(
            id=st.new_id("RetrieveRequest"),
            zip_file=buf.getvalue(),
            file_properties=tuple(props),
            messages=tuple(messages),
        )

    def retrieve(
        self,
        package: str | bytes | Mapping[str, Iterable[str]],
        *,
        extract_to: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> RetrieveResult:
        """A zip of the seeded components that ``package`` names, built at once."""
        package_types(package)  # validate before the call is recorded
        _Clock(timeout)
        result = self._state.run(
            self._who,
            "metadata.retrieve",
            None,
            {"package": package},
            lambda: self._retrieve(package),
            method="POST",
            path=metadata_soap_path(self._state.api_version),
        )
        if extract_to is not None:
            _extract(result.zip_file, os.fspath(extract_to))
        return result

    def list_metadata(self, metadata_type: str, *, folder: str | None = None) -> list[FileProperties]:
        """The seeded components of ``metadata_type`` (in ``folder``, for folder types)."""
        list_metadata_envelope(metadata_type, folder, self._state.api_version, "")  # same validation

        def fn() -> list[FileProperties]:
            return [
                _file_properties(mtype, full_name, comp)
                for (mtype, full_name), comp in sorted(self._state.metadata_components.items())
                if mtype == metadata_type and (folder is None or full_name.startswith(folder + "/"))
            ]

        return self._state.run(
            self._who,
            "metadata.list",
            None,
            {"type": metadata_type, "folder": folder},
            fn,
            method="POST",
            path=metadata_soap_path(self._state.api_version),
        )

    def describe_metadata(self) -> MetadataDescribe:
        """One :class:`MetadataType` per seeded type, derived from its file paths."""

        def fn() -> MetadataDescribe:
            by_type: dict[str, list[tuple[str, dict[str, bytes]]]] = {}
            for (mtype, full_name), comp in sorted(self._state.metadata_components.items()):
                by_type.setdefault(mtype, []).append((full_name, comp))
            types: list[MetadataType] = []
            for mtype, comps in by_type.items():
                paths = [p for _, comp in comps for p in comp]
                main = next((p for p in paths if not p.endswith("-meta.xml")), paths[0])
                ext = os.path.splitext(main.removesuffix("-meta.xml"))[1].lstrip(".")
                types.append(
                    MetadataType(
                        xml_name=mtype,
                        directory_name=main.split("/", 1)[0] if "/" in main else None,
                        suffix=ext or None,
                        in_folder=any("/" in full_name for full_name, _ in comps),
                        meta_file=any(p.endswith("-meta.xml") for p in paths),
                    )
                )
            return MetadataDescribe(types=tuple(types), partial_save_allowed=True, test_required=False)

        return self._state.run(
            self._who,
            "metadata.describe",
            None,
            {},
            fn,
            method="POST",
            path=metadata_soap_path(self._state.api_version),
        )


def _not_found(deploy_id: str) -> NotFound:
    return NotFound(
        f"no deployment with id {deploy_id}",
        error_code="NOT_FOUND",
        status=404,
        operation="metadata.deploy_status",
    )


class AsyncFakeMetadata:
    """Async twin of :class:`FakeMetadata`."""

    def __init__(self, inner: FakeMetadata) -> None:
        self._inner = inner

    async def deploy(
        self, source: bytes | str | os.PathLike[str], *, options: DeployOptions | None = None
    ) -> DeployResult:
        """See :meth:`FakeMetadata.deploy`."""
        return self._inner.deploy(source, options=options)

    async def deploy_status(
        self, deploy_id: str, *, wait: bool = False, timeout: float = DEFAULT_TIMEOUT
    ) -> DeployResult:
        """See :meth:`FakeMetadata.deploy_status`."""
        return self._inner.deploy_status(deploy_id, wait=wait, timeout=timeout)

    async def retrieve(
        self,
        package: str | bytes | Mapping[str, Iterable[str]],
        *,
        extract_to: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> RetrieveResult:
        """See :meth:`FakeMetadata.retrieve`."""
        return self._inner.retrieve(package, extract_to=extract_to, timeout=timeout)

    async def list_metadata(self, metadata_type: str, *, folder: str | None = None) -> list[FileProperties]:
        """See :meth:`FakeMetadata.list_metadata`."""
        return self._inner.list_metadata(metadata_type, folder=folder)

    async def describe_metadata(self) -> MetadataDescribe:
        """See :meth:`FakeMetadata.describe_metadata`."""
        return self._inner.describe_metadata()
