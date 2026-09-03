"""Input preparation, safety preflight, and content-based format detection."""

from __future__ import annotations

import hashlib
import io
import ntpath
import os
import stat
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO, TypeAlias, cast

from pydantic import Field, model_validator

from document_parser.exceptions import (
    DocumentParserError,
    InvalidDocumentError,
    SourceNotFoundError,
    SourceReadError,
    SourceTooLargeError,
    UnsafeDocumentError,
    UnsupportedFormatError,
)
from document_parser.models import (
    Diagnostic,
    DiagnosticSeverity,
    DocumentFormat,
    FrozenModel,
    SourceInfo,
)

MEBIBYTE = 1024 * 1024
READ_CHUNK_SIZE = 64 * 1024
CONTENT_TYPES_MAX_BYTES = MEBIBYTE
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
PDF_SIGNATURE = b"%PDF-"
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

SourceInput: TypeAlias = str | os.PathLike[str] | bytes | bytearray | BinaryIO


class ParseOptions(FrozenModel):
    """Safety limits applied before a format adapter is selected."""

    max_input_bytes: int = Field(default=100 * MEBIBYTE, gt=0)
    spool_threshold_bytes: int = Field(default=8 * MEBIBYTE, gt=0)
    max_archive_entries: int = Field(default=10_000, gt=0)
    max_archive_uncompressed_bytes: int = Field(default=1024 * MEBIBYTE, gt=0)
    max_archive_compression_ratio: float = Field(default=100.0, gt=0)
    strict_extension: bool = False

    @model_validator(mode="after")
    def validate_spool_threshold(self) -> ParseOptions:
        if self.spool_threshold_bytes > self.max_input_bytes:
            raise ValueError("spool_threshold_bytes cannot exceed max_input_bytes")
        return self


class AdapterInput:
    """A seekable snapshot made available to a format adapter during parsing."""

    __slots__ = ("_stream", "info")

    def __init__(self, info: SourceInfo, stream: BinaryIO) -> None:
        self.info = info
        self._stream = stream

    @property
    def closed(self) -> bool:
        """Whether the parser has released the prepared source."""

        return self._stream.closed

    @contextmanager
    def open_binary(self) -> Iterator[BinaryIO]:
        """Borrow the prepared stream from position zero without closing it."""

        if self.closed:
            raise SourceReadError("prepared source is already closed", source_name=self.info.name)
        previous_position = self._stream.tell()
        self._stream.seek(0)
        try:
            yield self._stream
        finally:
            if not self.closed:
                self._stream.seek(previous_position)


def _safe_stream_name(stream: BinaryIO) -> str | None:
    raw_name = getattr(stream, "name", None)
    if not isinstance(raw_name, (str, os.PathLike)):
        return None
    path_text = os.fsdecode(raw_name)
    name = path_text.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    return name or None


def _validate_supplied_name(filename: str | None) -> str | None:
    if filename is None:
        return None
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise SourceReadError("filename must be a non-empty basename")
    return filename


def _copy_binary_stream(
    reader: BinaryIO,
    destination: BinaryIO,
    *,
    options: ParseOptions,
    source_name: str | None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        try:
            chunk = reader.read(READ_CHUNK_SIZE)
        except (OSError, ValueError) as exc:
            raise SourceReadError("could not read binary source", source_name=source_name) from exc
        if chunk == b"" or chunk is None:
            break
        if not isinstance(chunk, bytes):
            raise SourceReadError("source stream must return bytes", source_name=source_name)
        size += len(chunk)
        if size > options.max_input_bytes:
            raise SourceTooLargeError(
                f"source exceeds max_input_bytes={options.max_input_bytes}",
                source_name=source_name,
            )
        destination.write(chunk)
        digest.update(chunk)
    if size == 0:
        raise InvalidDocumentError("source is empty", source_name=source_name)
    destination.seek(0)
    return size, digest.hexdigest()


def _copy_user_stream(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    options: ParseOptions,
    source_name: str | None,
) -> tuple[int, str]:
    original_position: int | None = None
    try:
        if source.seekable():
            original_position = source.tell()
            source.seek(0)
    except (OSError, ValueError):
        original_position = None
    try:
        return _copy_binary_stream(
            source,
            destination,
            options=options,
            source_name=source_name,
        )
    finally:
        if original_position is not None:
            with suppress(OSError, ValueError):
                source.seek(original_position)


def _extension(name: str | None) -> str | None:
    if name is None:
        return None
    suffix = Path(name).suffix.lower()
    return suffix or None


def _unsafe_archive(message: str, source_name: str | None) -> UnsafeDocumentError:
    return UnsafeDocumentError(message, source_name=source_name)


def _validate_archive(
    archive: zipfile.ZipFile,
    *,
    options: ParseOptions,
    source_name: str | None,
) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > options.max_archive_entries:
        raise _unsafe_archive("archive contains too many entries", source_name)

    normalized_entries: dict[str, zipfile.ZipInfo] = {}
    seen_names: set[str] = set()
    total_uncompressed = 0
    for entry in entries:
        normalized_name = entry.filename.replace("\\", "/")
        raw_parts = normalized_name.split("/")
        folded_name = normalized_name.casefold()
        if (
            not normalized_name
            or normalized_name.startswith("/")
            or ntpath.splitdrive(normalized_name)[0]
            or any(part in {".", ".."} for part in raw_parts)
        ):
            raise _unsafe_archive("archive contains an unsafe entry path", source_name)
        if folded_name in seen_names:
            raise _unsafe_archive("archive contains duplicate entry names", source_name)
        seen_names.add(folded_name)
        normalized_entries[folded_name] = entry

        if entry.flag_bits & 0x1:
            raise _unsafe_archive("archive contains an encrypted entry", source_name)
        unix_mode = (entry.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(unix_mode):
            raise _unsafe_archive("archive contains a symbolic link", source_name)

        total_uncompressed += entry.file_size
        if total_uncompressed > options.max_archive_uncompressed_bytes:
            raise _unsafe_archive("archive expands beyond the configured limit", source_name)
        if entry.file_size:
            if entry.compress_size == 0:
                raise _unsafe_archive("archive entry has an invalid compression ratio", source_name)
            ratio = entry.file_size / entry.compress_size
            if ratio > options.max_archive_compression_ratio:
                raise _unsafe_archive(
                    "archive compression ratio exceeds the configured limit", source_name
                )
    return normalized_entries


def _read_content_types(
    archive: zipfile.ZipFile,
    entries: dict[str, zipfile.ZipInfo],
    *,
    source_name: str | None,
) -> bytes:
    manifest = entries.get("[content_types].xml")
    if manifest is None:
        return b""
    if manifest.file_size > CONTENT_TYPES_MAX_BYTES:
        raise _unsafe_archive("content-types manifest is unexpectedly large", source_name)
    try:
        return archive.read(manifest)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise InvalidDocumentError(
            "could not read the Office content-types manifest", source_name=source_name
        ) from exc


def _detect_zip_format(
    stream: BinaryIO,
    *,
    supplied_extension: str | None,
    options: ParseOptions,
    source_name: str | None,
) -> DocumentFormat:
    try:
        with zipfile.ZipFile(stream) as archive:
            entries = _validate_archive(archive, options=options, source_name=source_name)
            names = set(entries)
            content_types = _read_content_types(archive, entries, source_name=source_name).lower()
    except UnsafeDocumentError:
        raise
    except zipfile.BadZipFile as exc:
        raise InvalidDocumentError("ZIP container is malformed", source_name=source_name) from exc
    finally:
        stream.seek(0)

    macro_extension = supplied_extension in {".docm", ".xlsm"}
    macro_payload = any(name.endswith("/vbaproject.bin") for name in names)
    if macro_extension or macro_payload or b"macroenabled" in content_types:
        raise UnsupportedFormatError(
            "macro-enabled Office documents are not supported", source_name=source_name
        )

    common_parts = {"[content_types].xml", "_rels/.rels"}
    if common_parts.issubset(names) and "word/document.xml" in names:
        return DocumentFormat.DOCX
    if common_parts.issubset(names) and "xl/workbook.xml" in names:
        return DocumentFormat.XLSX
    if supplied_extension in {".docx", ".xlsx"}:
        raise InvalidDocumentError(
            "Office archive is missing required package parts", source_name=source_name
        )
    raise UnsupportedFormatError("ZIP container is not DOCX or XLSX", source_name=source_name)


def _detect_format(
    stream: BinaryIO,
    *,
    supplied_extension: str | None,
    options: ParseOptions,
    source_name: str | None,
) -> DocumentFormat:
    header = stream.read(8)
    stream.seek(0)
    if header.startswith(PDF_SIGNATURE):
        return DocumentFormat.PDF
    if header.startswith(OLE_SIGNATURE):
        raise UnsupportedFormatError(
            "legacy or encrypted Office containers are not supported", source_name=source_name
        )
    if zipfile.is_zipfile(stream):
        stream.seek(0)
        return _detect_zip_format(
            stream,
            supplied_extension=supplied_extension,
            options=options,
            source_name=source_name,
        )
    stream.seek(0)
    if header.startswith(ZIP_SIGNATURES) or supplied_extension in {".docx", ".xlsx"}:
        raise InvalidDocumentError("document container is malformed", source_name=source_name)
    raise UnsupportedFormatError("source format is not supported", source_name=source_name)


def _source_info(
    stream: BinaryIO,
    *,
    name: str | None,
    size_bytes: int,
    sha256: str,
    options: ParseOptions,
) -> SourceInfo:
    supplied_extension = _extension(name)
    document_format = _detect_format(
        stream,
        supplied_extension=supplied_extension,
        options=options,
        source_name=name,
    )
    expected_extension = f".{document_format.value}"
    extension_matches = (
        None if supplied_extension is None else supplied_extension == expected_extension
    )
    diagnostics: tuple[Diagnostic, ...] = ()
    if extension_matches is False:
        if options.strict_extension:
            raise InvalidDocumentError(
                f"source extension does not match detected {document_format.value} content",
                source_name=name,
            )
        diagnostics = (
            Diagnostic(
                code="source.extension_mismatch",
                message=(
                    f"Supplied extension {supplied_extension!r} does not match "
                    f"detected {document_format.value!r} content."
                ),
                severity=DiagnosticSeverity.WARNING,
                details={
                    "detected_format": document_format.value,
                    "supplied_extension": supplied_extension,
                },
            ),
        )

    media_types = {
        DocumentFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        DocumentFormat.PDF: "application/pdf",
        DocumentFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    resolved_name = name or f"document.{document_format.value}"
    return SourceInfo(
        name=resolved_name,
        size_bytes=size_bytes,
        sha256=sha256,
        format=document_format,
        media_type=media_types[document_format],
        supplied_extension=supplied_extension,
        extension_matches=extension_matches,
        diagnostics=diagnostics,
    )


@contextmanager
def prepare_source(
    source: SourceInput,
    *,
    filename: str | None,
    options: ParseOptions,
) -> Iterator[AdapterInput]:
    """Create a bounded, seekable snapshot and run format preflight."""

    supplied_name = _validate_supplied_name(filename)
    with tempfile.SpooledTemporaryFile(max_size=options.spool_threshold_bytes, mode="w+b") as spool:
        spool_stream = cast(BinaryIO, spool)
        if isinstance(source, (str, os.PathLike)):
            if supplied_name is not None:
                raise SourceReadError("filename cannot be used with a path source")
            path = Path(source)
            source_name = path.name or None
            if not path.exists():
                raise SourceNotFoundError("source path does not exist", source_name=source_name)
            if not path.is_file():
                raise SourceReadError("source path is not a file", source_name=source_name)
            try:
                if path.stat().st_size > options.max_input_bytes:
                    raise SourceTooLargeError(
                        f"source exceeds max_input_bytes={options.max_input_bytes}",
                        source_name=source_name,
                    )
                with path.open("rb") as reader:
                    size_bytes, sha256 = _copy_binary_stream(
                        reader,
                        spool_stream,
                        options=options,
                        source_name=source_name,
                    )
            except DocumentParserError:
                raise
            except OSError as exc:
                raise SourceReadError(
                    "could not open source path", source_name=source_name
                ) from exc
        elif isinstance(source, (bytes, bytearray)):
            source_name = supplied_name
            size_bytes, sha256 = _copy_binary_stream(
                io.BytesIO(bytes(source)),
                spool_stream,
                options=options,
                source_name=source_name,
            )
        elif hasattr(source, "read"):
            binary_source = source
            source_name = supplied_name or _safe_stream_name(binary_source)
            size_bytes, sha256 = _copy_user_stream(
                binary_source,
                spool_stream,
                options=options,
                source_name=source_name,
            )
        else:
            raise SourceReadError("source must be a path, bytes, or binary stream")

        info = _source_info(
            spool_stream,
            name=source_name,
            size_bytes=size_bytes,
            sha256=sha256,
            options=options,
        )
        yield AdapterInput(info, spool_stream)
