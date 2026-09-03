"""Tests for source preparation, safety checks, and format detection."""

from __future__ import annotations

import io
import stat
import struct
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import document_parser.sources as source_module
from document_parser import (
    DocumentFormat,
    DocumentParser,
    InvalidDocumentError,
    ParseOptions,
    SourceNotFoundError,
    SourceReadError,
    SourceTooLargeError,
    UnsafeDocumentError,
    UnsupportedFormatError,
    inspect_source,
)

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
OLE_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy"


def make_office_archive(
    document_format: DocumentFormat,
    *,
    entries: tuple[tuple[str, bytes], ...] = (),
    content_types: bytes = b"<Types/>",
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    buffer = io.BytesIO()
    core_path = "word/document.xml" if document_format is DocumentFormat.DOCX else "xl/workbook.xml"
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", b"<Relationships/>")
        archive.writestr(core_path, b"<root/>")
        for name, value in entries:
            archive.writestr(name, value)
    return buffer.getvalue()


def make_unknown_zip(*, filename: str = "data.txt", value: bytes = b"data") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(filename, value)
    return buffer.getvalue()


def mark_zip_encrypted(payload: bytes) -> bytes:
    patched = bytearray(payload)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = 0
        while (position := patched.find(signature, position)) != -1:
            flags = struct.unpack_from("<H", patched, position + flag_offset)[0]
            struct.pack_into("<H", patched, position + flag_offset, flags | 0x1)
            position += 4
    return bytes(patched)


def zero_first_central_compressed_size(payload: bytes) -> bytes:
    patched = bytearray(payload)
    position = patched.index(b"PK\x01\x02")
    struct.pack_into("<I", patched, position + 20, 0)
    return bytes(patched)


class NamedBytesIO(io.BytesIO):
    name = "C:\\folder\\report.PDF"


class NonSeekableStream:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def seekable(self) -> bool:
        return False


class TextReturningStream:
    def read(self, size: int = -1) -> str:
        return "not binary"

    def seekable(self) -> bool:
        return False


class BrokenStream:
    def read(self, size: int = -1) -> bytes:
        raise OSError("read failed")

    def seekable(self) -> bool:
        return False


class RestoreFailingStream(io.BytesIO):
    def seek(self, offset: int, whence: int = 0) -> int:
        if offset == 2 and whence == 0:
            raise OSError("restore failed")
        return super().seek(offset, whence)


class EmptyNamedStream(io.BytesIO):
    name = ""


def test_inspect_pdf_bytes_without_filename_uses_content_identity() -> None:
    info = inspect_source(PDF_BYTES)

    assert info.name == "document.pdf"
    assert info.format is DocumentFormat.PDF
    assert info.media_type == "application/pdf"
    assert info.supplied_extension is None
    assert info.extension_matches is None
    assert info.size_bytes == len(PDF_BYTES)
    assert len(info.sha256) == 64
    assert info.diagnostics == ()


def test_bytearray_and_path_inputs_are_supported(tmp_path: Path) -> None:
    assert inspect_source(bytearray(PDF_BYTES), filename="bytes.pdf").format is DocumentFormat.PDF

    path = tmp_path / "source.pdf"
    path.write_bytes(PDF_BYTES)
    info = DocumentParser().inspect(path)

    assert info.name == "source.pdf"
    assert info.extension_matches is True
    assert inspect_source(str(path)) == info


def test_seekable_and_non_seekable_stream_contracts() -> None:
    seekable = NamedBytesIO(PDF_BYTES)
    seekable.seek(7)
    info = inspect_source(seekable)
    assert seekable.tell() == 7
    assert not seekable.closed
    assert info.name == "report.PDF"
    assert info.supplied_extension == ".pdf"

    non_seekable = NonSeekableStream(PDF_BYTES)
    assert inspect_source(non_seekable, filename="stream.pdf").format is DocumentFormat.PDF  # type: ignore[arg-type]
    assert inspect_source(NonSeekableStream(PDF_BYTES)).name == "document.pdf"  # type: ignore[arg-type]
    assert inspect_source(EmptyNamedStream(PDF_BYTES)).name == "document.pdf"


def test_failed_stream_position_restore_does_not_hide_valid_input() -> None:
    stream = RestoreFailingStream(PDF_BYTES)
    io.BytesIO.seek(stream, 2)
    info = inspect_source(stream, filename="restore.pdf")
    assert info.format is DocumentFormat.PDF


def test_extension_mismatch_warns_or_fails_in_strict_mode() -> None:
    info = inspect_source(PDF_BYTES, filename="actually-docx.docx")
    diagnostic = info.diagnostics[0]
    assert info.format is DocumentFormat.PDF
    assert info.extension_matches is False
    assert diagnostic.code == "source.extension_mismatch"
    assert diagnostic.details["detected_format"] == "pdf"

    with pytest.raises(InvalidDocumentError, match="extension does not match"):
        inspect_source(
            PDF_BYTES,
            filename="actually-docx.docx",
            options=ParseOptions(strict_extension=True),
        )


@pytest.mark.parametrize(
    ("document_format", "filename", "expected_media_type"),
    [
        (
            DocumentFormat.DOCX,
            "sample.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            DocumentFormat.XLSX,
            "sample.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ],
)
def test_office_formats_are_detected_from_required_package_parts(
    document_format: DocumentFormat,
    filename: str,
    expected_media_type: str,
) -> None:
    info = inspect_source(make_office_archive(document_format), filename=filename)
    assert info.format is document_format
    assert info.media_type == expected_media_type
    assert info.extension_matches is True


@pytest.mark.parametrize(
    ("payload", "filename"),
    [
        (make_office_archive(DocumentFormat.DOCX), "sample.docm"),
        (
            make_office_archive(
                DocumentFormat.XLSX,
                entries=(("xl/vbaProject.bin", b"macro"),),
            ),
            "sample.xlsx",
        ),
        (
            make_office_archive(
                DocumentFormat.DOCX,
                content_types=b"<Types>macroEnabled</Types>",
            ),
            "sample.docx",
        ),
    ],
)
def test_macro_enabled_office_documents_are_rejected(payload: bytes, filename: str) -> None:
    with pytest.raises(UnsupportedFormatError, match="macro-enabled"):
        inspect_source(payload, filename=filename)


@pytest.mark.parametrize(
    ("source", "filename", "exception", "message"),
    [
        (b"", "empty.pdf", InvalidDocumentError, "empty"),
        (b"plain text", "notes.txt", UnsupportedFormatError, "not supported"),
        (b"PK\x03\x04broken", "broken.docx", InvalidDocumentError, "malformed"),
        (OLE_BYTES, "legacy.doc", UnsupportedFormatError, "Office containers"),
        (make_unknown_zip(), None, UnsupportedFormatError, "not DOCX or XLSX"),
        (make_unknown_zip(), "fake.docx", InvalidDocumentError, "required package parts"),
    ],
)
def test_invalid_or_unsupported_inputs_have_typed_errors(
    source: bytes,
    filename: str | None,
    exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(exception, match=message):
        inspect_source(source, filename=filename)


def test_path_and_filename_errors_are_typed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pdf"
    with pytest.raises(SourceNotFoundError, match="does not exist") as missing_error:
        inspect_source(missing)
    assert missing_error.value.source_name == "missing.pdf"

    with pytest.raises(SourceReadError, match="not a file"):
        inspect_source(tmp_path)

    path = tmp_path / "valid.pdf"
    path.write_bytes(PDF_BYTES)
    with pytest.raises(SourceReadError, match="cannot be used with a path"):
        inspect_source(path, filename="override.pdf")


@pytest.mark.parametrize("filename", ["", "folder/name.pdf", "folder\\name.pdf"])
def test_explicit_filename_must_be_a_basename(filename: str) -> None:
    with pytest.raises(SourceReadError, match="basename"):
        inspect_source(PDF_BYTES, filename=filename)


def test_input_size_limits_apply_before_and_during_copy(tmp_path: Path) -> None:
    options = ParseOptions(max_input_bytes=8, spool_threshold_bytes=4)
    with pytest.raises(SourceTooLargeError, match="max_input_bytes"):
        inspect_source(PDF_BYTES, options=options)

    path = tmp_path / "large.pdf"
    path.write_bytes(PDF_BYTES)
    with pytest.raises(SourceTooLargeError, match="max_input_bytes"):
        inspect_source(path, options=options)

    with pytest.raises(ValidationError, match="cannot exceed"):
        ParseOptions(max_input_bytes=4, spool_threshold_bytes=5)


@pytest.mark.parametrize("stream", [TextReturningStream(), BrokenStream(), io.BytesIO()])
def test_unreadable_streams_raise_source_errors(stream: Any) -> None:
    if isinstance(stream, io.BytesIO):
        stream.close()
    with pytest.raises(SourceReadError, match=r"source stream|could not read"):
        inspect_source(stream, filename="broken.pdf")


def test_non_source_object_is_rejected() -> None:
    with pytest.raises(SourceReadError, match="path, bytes, or binary stream"):
        inspect_source(42)  # type: ignore[arg-type]


@pytest.mark.parametrize("entry_name", ["../evil", "/absolute", "C:/drive", "./relative"])
def test_archive_paths_are_validated(entry_name: str) -> None:
    payload = make_office_archive(
        DocumentFormat.DOCX,
        entries=((entry_name, b"unsafe"),),
    )
    with pytest.raises(UnsafeDocumentError, match="unsafe entry path"):
        inspect_source(payload, filename="unsafe.docx")


def test_archive_rejects_duplicate_names_and_symlinks() -> None:
    duplicate = make_office_archive(
        DocumentFormat.DOCX,
        entries=(("WORD/DOCUMENT.XML", b"duplicate"),),
    )
    with pytest.raises(UnsafeDocumentError, match="duplicate"):
        inspect_source(duplicate, filename="duplicate.docx")

    buffer = io.BytesIO()
    symlink = zipfile.ZipInfo("word/link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("_rels/.rels", b"<Relationships/>")
        archive.writestr("word/document.xml", b"<root/>")
        archive.writestr(symlink, b"target")
    with pytest.raises(UnsafeDocumentError, match="symbolic link"):
        inspect_source(buffer.getvalue(), filename="symlink.docx")


def test_archive_allows_zero_length_regular_entries() -> None:
    payload = make_office_archive(
        DocumentFormat.DOCX,
        entries=(("word/empty.xml", b""),),
    )
    assert inspect_source(payload, filename="empty-entry.docx").format is DocumentFormat.DOCX


def test_archive_rejects_encryption_and_invalid_zero_compressed_size() -> None:
    payload = make_office_archive(DocumentFormat.DOCX)
    with pytest.raises(UnsafeDocumentError, match="encrypted entry"):
        inspect_source(mark_zip_encrypted(payload), filename="encrypted.docx")
    with pytest.raises(UnsafeDocumentError, match="invalid compression ratio"):
        inspect_source(zero_first_central_compressed_size(payload), filename="ratio.docx")


def test_archive_limits_cover_entry_count_size_and_ratio() -> None:
    payload = make_office_archive(DocumentFormat.DOCX)
    with pytest.raises(UnsafeDocumentError, match="too many entries"):
        inspect_source(payload, filename="many.docx", options=ParseOptions(max_archive_entries=2))
    with pytest.raises(UnsafeDocumentError, match="expands beyond"):
        inspect_source(
            payload,
            filename="large.docx",
            options=ParseOptions(max_archive_uncompressed_bytes=5),
        )

    compressed = make_office_archive(
        DocumentFormat.XLSX,
        content_types=b"A" * 1000,
        compression=zipfile.ZIP_DEFLATED,
    )
    with pytest.raises(UnsafeDocumentError, match="compression ratio exceeds"):
        inspect_source(
            compressed,
            filename="compressed.xlsx",
            options=ParseOptions(max_archive_compression_ratio=1),
        )


def test_content_types_manifest_has_its_own_size_and_integrity_limits() -> None:
    oversized = make_office_archive(
        DocumentFormat.DOCX,
        content_types=b"A" * (1024 * 1024 + 1),
    )
    with pytest.raises(UnsafeDocumentError, match="manifest is unexpectedly large"):
        inspect_source(oversized, filename="manifest.docx")

    valid = make_office_archive(DocumentFormat.DOCX, content_types=b"unique-manifest")
    corrupted = bytearray(valid)
    position = corrupted.index(b"unique-manifest")
    corrupted[position] ^= 0xFF
    with pytest.raises(InvalidDocumentError, match="could not read"):
        inspect_source(bytes(corrupted), filename="corrupt.docx")


def test_bad_zip_during_archive_open_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_bad_zip(*args: object, **kwargs: object) -> dict[str, zipfile.ZipInfo]:
        raise zipfile.BadZipFile("bad central directory")

    monkeypatch.setattr(source_module, "_validate_archive", raise_bad_zip)
    payload = make_office_archive(DocumentFormat.DOCX)
    with pytest.raises(InvalidDocumentError, match="ZIP container is malformed"):
        inspect_source(payload, filename="bad.docx")


def test_path_open_oserror_is_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "unreadable.pdf"
    path.write_bytes(PDF_BYTES)

    def fail_open(self: Path, *args: object, **kwargs: object) -> Any:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(SourceReadError, match="could not open source path"):
        inspect_source(path)
