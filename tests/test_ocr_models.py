"""Tests for explicit offline OCR model preparation and verification."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any, cast

import pytest
from pydantic import ValidationError

from document_parser import (
    OcrConfigurationError,
    OcrDependencyNotAvailableError,
    OcrModelFile,
    OcrModelManifest,
    OcrModelNotAvailableError,
    OcrModelRecord,
    OcrProfile,
    UnsafeDocumentError,
    prepare_ocr_models,
    verify_ocr_models,
)
from document_parser.ocr_models import (
    MODEL_MANIFEST_NAME,
    _copy_download,
    _download,
    _extract,
    _file_records,
    _safe_members,
    required_model_names,
    resolve_model_store,
)


def model_record(name: str, data: bytes = b"model") -> OcrModelRecord:
    return OcrModelRecord(
        name=name,
        source_url=f"https://example.test/{name}.tar",
        archive_sha256="a" * 64,
        archive_size_bytes=1,
        files=(
            OcrModelFile(
                path="inference.json",
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            ),
        ),
    )


def write_store(target: Path, record: OcrModelRecord, data: bytes = b"model") -> None:
    model_dir = target / record.name
    model_dir.mkdir(parents=True)
    (model_dir / "inference.json").write_bytes(data)
    manifest = OcrModelManifest(
        profiles=(OcrProfile.TEXT,),
        languages=("az",),
        models=(record,),
    )
    (target / MODEL_MANIFEST_NAME).write_text(manifest.model_dump_json(), encoding="utf-8")


def test_required_model_names_are_profile_and_language_specific() -> None:
    text = required_model_names((OcrProfile.TEXT,), ("az", "en"))
    structured = required_model_names((OcrProfile.STRUCTURED,), ("az", "ru"))
    assert "PP-OCRv6_small_det" in text
    assert "PP-OCRv6_medium_det" not in text
    assert "PP-DocLayoutV3" in structured
    assert "eslav_PP-OCRv5_mobile_rec" in structured


def test_model_manifest_validation_rejects_unsafe_and_duplicate_entries() -> None:
    with pytest.raises(ValidationError, match="relative and safe"):
        model_record("unsafe").model_copy(
            update={"files": (OcrModelFile(path="../x", size_bytes=0, sha256="a" * 64),)}
        )
        OcrModelRecord(
            name="unsafe",
            source_url="https://example.test/x",
            archive_sha256="a" * 64,
            archive_size_bytes=1,
            files=(OcrModelFile(path="../x", size_bytes=0, sha256="a" * 64),),
        )
    record = model_record("one")
    with pytest.raises(ValidationError, match="unique and sorted"):
        OcrModelRecord(
            name="duplicate",
            source_url="https://example.test/x",
            archive_sha256="a" * 64,
            archive_size_bytes=1,
            files=(record.files[0], record.files[0]),
        )
    with pytest.raises(ValidationError, match="size limit"):
        model_record("large").model_copy(
            update={"archive_size_bytes": 2, "archive_size_limit_bytes": 1}
        )
        OcrModelRecord(
            name="large",
            source_url="https://example.test/large.tar",
            archive_sha256="a" * 64,
            archive_size_bytes=2,
            archive_size_limit_bytes=1,
            files=record.files,
        )
    with pytest.raises(ValidationError, match="unique sorted names"):
        OcrModelManifest(
            profiles=(OcrProfile.TEXT,),
            languages=("az",),
            models=(record, record),
        )


def test_verify_model_store_reports_valid_missing_and_corrupt(tmp_path: Path) -> None:
    record = model_record("model")
    write_store(tmp_path, record)
    report = verify_ocr_models(tmp_path)
    assert report.valid
    assert report.verified_models == ("model",)

    requested = verify_ocr_models(tmp_path, required=("missing", "model"))
    assert not requested.valid and requested.missing == ("missing",)

    (tmp_path / "model" / "inference.json").write_bytes(b"bad")
    corrupt = verify_ocr_models(tmp_path)
    assert not corrupt.valid and corrupt.corrupted == ("model",)

    (tmp_path / MODEL_MANIFEST_NAME).write_text("broken", encoding="utf-8")
    missing_manifest = verify_ocr_models(tmp_path)
    assert missing_manifest.missing == (MODEL_MANIFEST_NAME,)


def test_resolve_model_store_uses_explicit_path_and_reports_missing_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert resolve_model_store(tmp_path) == tmp_path.resolve()

    def missing(_name: str) -> object:
        raise ImportError("missing")

    monkeypatch.setattr("document_parser.ocr_models.import_module", missing)
    with pytest.raises(OcrDependencyNotAvailableError, match="optional dependencies"):
        resolve_model_store()

    monkeypatch.setattr(
        "document_parser.ocr_models.import_module",
        lambda _name: SimpleNamespace(user_cache_path=lambda *_args, **_kwargs: tmp_path),
    )
    assert resolve_model_store() == tmp_path / "models"


def test_safe_tar_validation_rejects_paths_links_devices_and_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def archive_with(name: str, *, kind: bytes = tarfile.REGTYPE, size: int = 1) -> Path:
        path = tmp_path / f"{len(tuple(tmp_path.iterdir()))}.tar"
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = size
            archive.addfile(info, io.BytesIO(b"x" * size) if kind == tarfile.REGTYPE else None)
        return path

    for name in ("../x", "/absolute", "C:/drive"):
        with (
            tarfile.open(archive_with(name)) as archive,
            pytest.raises(UnsafeDocumentError, match="unsafe entry"),
        ):
            _safe_members(archive)
    for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE):
        with (
            tarfile.open(archive_with("unsafe", kind=kind)) as archive,
            pytest.raises(UnsafeDocumentError, match="unsafe entry"),
        ):
            _safe_members(archive)

    monkeypatch.setattr("document_parser.ocr_models.MAX_MODEL_FILES", 0)
    with (
        tarfile.open(archive_with("file")) as archive,
        pytest.raises(UnsafeDocumentError, match="too many"),
    ):
        _safe_members(archive)
    monkeypatch.setattr("document_parser.ocr_models.MAX_MODEL_FILES", 10)
    monkeypatch.setattr("document_parser.ocr_models.MAX_MODEL_UNPACKED_BYTES", 0)
    with (
        tarfile.open(archive_with("large")) as archive,
        pytest.raises(UnsafeDocumentError, match="expands"),
    ):
        _safe_members(archive)


def test_prepare_models_is_explicit_atomic_and_checksum_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "fixture.tar"
    with tarfile.open(archive, "w") as bundle:
        info = tarfile.TarInfo("fixture_infer/inference.json")
        info.size = len(b"payload")
        bundle.addfile(info, io.BytesIO(b"payload"))
    archive_data = archive.read_bytes()

    def fake_download(_url: str, destination: Path) -> str:
        destination.write_bytes(archive_data)
        return hashlib.sha256(archive_data).hexdigest()

    monkeypatch.setattr(
        "document_parser.ocr_models.required_model_names", lambda *_args: ("fixture",)
    )
    monkeypatch.setattr("document_parser.ocr_models._download", fake_download)
    store = tmp_path / "models"
    report = prepare_ocr_models(
        store,
        profiles=(OcrProfile.TEXT,),
        languages=("AZ", "az"),
    )
    assert report.valid
    assert (store / "fixture" / "inference.json").read_bytes() == b"payload"
    manifest = OcrModelManifest.model_validate_json(
        (store / MODEL_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest.languages == ("az",)
    assert manifest.models[0].archive_sha256 == hashlib.sha256(archive_data).hexdigest()
    assert manifest.models[0].archive_size_bytes == len(archive_data)
    assert manifest.models[0].engine_compatibility == "paddleocr>=3.7,<4"

    multi_archive = tmp_path / "multi.tar"
    with tarfile.open(multi_archive, "w") as bundle:
        for name in ("inference.json", "config.yml"):
            info = tarfile.TarInfo(name)
            info.size = 1
            bundle.addfile(info, io.BytesIO(b"x"))
    multi_data = multi_archive.read_bytes()

    def multi_download(_url: str, destination: Path) -> str:
        destination.write_bytes(multi_data)
        return hashlib.sha256(multi_data).hexdigest()

    monkeypatch.setattr("document_parser.ocr_models._download", multi_download)
    replaced = prepare_ocr_models(store, profiles=(OcrProfile.TEXT,), languages=("az",))
    assert replaced.valid
    assert (store / "fixture" / "config.yml").is_file()

    with pytest.raises(OcrModelNotAvailableError, match="at least one OCR profile"):
        prepare_ocr_models(store, profiles=(), languages=("az",))
    with pytest.raises(OcrModelNotAvailableError, match="at least one OCR language"):
        prepare_ocr_models(store, profiles=(OcrProfile.TEXT,), languages=())
    with pytest.raises(OcrConfigurationError, match="unknown OCR profile"):
        prepare_ocr_models(store, profiles=("unknown",), languages=("az",))  # type: ignore[arg-type]
    with pytest.raises(OcrConfigurationError, match="does not support"):
        prepare_ocr_models(store, profiles=(OcrProfile.TEXT,), languages=("de",))


def test_download_and_extract_failure_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(UnsafeDocumentError, match="HTTPS"):
        _download("http://example.test/model", tmp_path / "model.tar")

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

    monkeypatch.setattr(
        "document_parser.ocr_models.urlopen", lambda *_args, **_kwargs: Response(b"x")
    )
    downloaded = tmp_path / "downloaded.tar"
    assert _download("https://example.test/model", downloaded) == hashlib.sha256(b"x").hexdigest()

    monkeypatch.setattr("document_parser.ocr_models.MAX_MODEL_ARCHIVE_BYTES", 0)
    with pytest.raises(UnsafeDocumentError, match="size limit"):
        _copy_download(io.BytesIO(b"x"), io.BytesIO())
    with pytest.raises(UnsafeDocumentError, match="size limit"):
        _download("https://example.test/large", tmp_path / "large.tar")

    broken = tmp_path / "broken.tar"
    broken.write_bytes(b"not a tar")
    with pytest.raises(OcrModelNotAvailableError, match="invalid"):
        _extract(broken, tmp_path / "out", "broken")

    unsafe = tmp_path / "unsafe.tar"
    with tarfile.open(unsafe, "w") as archive:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(UnsafeDocumentError, match="unsafe entry"):
        _extract(unsafe, tmp_path / "unsafe-out", "unsafe")


def test_download_wraps_io_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object, **_kwargs: object) -> object:
        raise OSError("offline")

    monkeypatch.setattr("document_parser.ocr_models.urlopen", broken)
    with pytest.raises(OcrModelNotAvailableError, match="could not download"):
        _download("https://example.test/model", tmp_path / "model.tar")


def test_model_file_inventory_and_verification_failure_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(OcrModelNotAvailableError, match="empty"):
        _file_records(empty)

    record = model_record("model")
    write_store(tmp_path / "missing-dir", record)
    (tmp_path / "missing-dir" / "model" / "inference.json").unlink()
    (tmp_path / "missing-dir" / "model").rmdir()
    assert verify_ocr_models(tmp_path / "missing-dir").missing == ("model",)

    missing_file = tmp_path / "missing-file"
    write_store(missing_file, record)
    (missing_file / "model" / "inference.json").unlink()
    assert verify_ocr_models(missing_file).corrupted == ("model",)

    wrong_digest = tmp_path / "wrong-digest"
    write_store(wrong_digest, record, data=b"other")
    assert verify_ocr_models(wrong_digest).corrupted == ("model",)

    unreadable = tmp_path / "unreadable"
    write_store(unreadable, record)
    original_open = Path.open

    def fail_model_open(self: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        if self.name == "inference.json":
            raise OSError("unreadable")
        return cast(IO[Any], original_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", fail_model_open)
    assert verify_ocr_models(unreadable).corrupted == ("model",)
