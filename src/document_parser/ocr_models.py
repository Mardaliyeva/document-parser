"""Explicit, checksum-recorded local model preparation for OCR."""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
from collections.abc import Iterable
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import Field, model_validator

from document_parser.exceptions import (
    OcrConfigurationError,
    OcrDependencyNotAvailableError,
    OcrModelNotAvailableError,
    UnsafeDocumentError,
)
from document_parser.models import FrozenModel
from document_parser.ocr import OcrProfile

MODEL_MANIFEST_NAME = "document-parser-models.json"
MODEL_MANIFEST_VERSION = "0.1"
MODEL_BASE_URL = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0"
)
MAX_MODEL_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MODEL_FILES = 10_000
MAX_MODEL_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024

_COMMON_MODELS = (
    "PP-LCNet_x1_0_doc_ori",
    "PP-LCNet_x1_0_textline_ori",
    "eslav_PP-OCRv5_mobile_rec",
)
_TEXT_MODELS = (
    "PP-OCRv6_small_det",
    "PP-OCRv6_small_rec",
)
_STRUCTURED_MODELS = (
    "UVDoc",
    "PP-DocLayoutV3",
    "PP-OCRv6_medium_det",
    "PP-OCRv6_medium_rec",
    "PP-LCNet_x1_0_table_cls",
    "SLANeXt_wired",
    "SLANeXt_wireless",
    "RT-DETR-L_wired_table_cell_det",
    "RT-DETR-L_wireless_table_cell_det",
)


class OcrModelFile(FrozenModel):
    """One hashed file inside a prepared local model directory."""

    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OcrModelRecord(FrozenModel):
    """Origin and local content inventory for one prepared model."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    model_release: Literal["paddle3.0.0"] = "paddle3.0.0"
    source_url: str = Field(pattern=r"^https://")
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size_bytes: int = Field(gt=0)
    archive_size_limit_bytes: int = Field(default=MAX_MODEL_ARCHIVE_BYTES, gt=0)
    engine_compatibility: Literal["paddleocr>=3.7,<4"] = "paddleocr>=3.7,<4"
    code_license: Literal["Apache-2.0"] = "Apache-2.0"
    weight_license: Literal["Apache-2.0"] = "Apache-2.0"
    notice: str = "PaddleOCR model distributed by PaddlePaddle under Apache-2.0."
    files: tuple[OcrModelFile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_paths(self) -> OcrModelRecord:
        if self.archive_size_bytes > self.archive_size_limit_bytes:
            raise ValueError("model archive exceeds its recorded size limit")
        paths = tuple(file.path for file in self.files)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("model file paths must be unique and sorted")
        for path in paths:
            candidate = PurePosixPath(path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError("model file path must be relative and safe")
        return self


class OcrModelManifest(FrozenModel):
    """Deterministic manifest written after explicit model preparation."""

    schema_version: Literal["0.1"] = "0.1"
    profiles: tuple[OcrProfile, ...]
    languages: tuple[str, ...]
    models: tuple[OcrModelRecord, ...]

    @model_validator(mode="after")
    def validate_models(self) -> OcrModelManifest:
        names = tuple(model.name for model in self.models)
        if names != tuple(sorted(set(names))):
            raise ValueError("model records must have unique sorted names")
        return self


class OcrModelReport(FrozenModel):
    """Read-only verification result for a local model store."""

    target: Path
    valid: bool
    verified_models: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    corrupted: tuple[str, ...] = ()


def required_model_names(
    profiles: Iterable[OcrProfile], languages: Iterable[str]
) -> tuple[str, ...]:
    """Resolve the exact local model set for profiles and language codes."""

    profile_set = set(profiles)
    language_set = {language.lower() for language in languages}
    names = set(_COMMON_MODELS[:2])
    if OcrProfile.TEXT in profile_set:
        names.update(_TEXT_MODELS)
    if OcrProfile.STRUCTURED in profile_set:
        names.update(_STRUCTURED_MODELS)
    if "ru" in language_set:
        names.add(_COMMON_MODELS[2])
    return tuple(sorted(names))


def resolve_model_store(target: str | os.PathLike[str] | None = None) -> Path:
    """Return an explicit path or the platform-specific application cache path."""

    if target is not None:
        return Path(target).expanduser().resolve()
    try:
        platformdirs = import_module("platformdirs")
        user_cache_path = platformdirs.user_cache_path
    except ImportError as exc:
        raise OcrDependencyNotAvailableError(
            "default OCR model storage requires the 'ocr' optional dependencies"
        ) from exc
    return Path(str(user_cache_path("document-parser", ensure_exists=False))) / "models"


def _model_url(name: str) -> str:
    return f"{MODEL_BASE_URL}/{name}_infer.tar"


def _copy_download(reader: BinaryIO, destination: BinaryIO) -> str:
    digest = hashlib.sha256()
    size = 0
    while chunk := reader.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_MODEL_ARCHIVE_BYTES:
            raise UnsafeDocumentError("OCR model archive exceeds the configured size limit")
        destination.write(chunk)
        digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> str:
    if urlparse(url).scheme != "https":
        raise UnsafeDocumentError("OCR model downloads require HTTPS")
    request = Request(url, headers={"User-Agent": "document-parser-model-preparer/0.4"})
    try:
        with urlopen(request, timeout=60) as response, destination.open("wb") as output:
            return _copy_download(response, output)
    except (OSError, TimeoutError) as exc:
        raise OcrModelNotAvailableError(f"could not download OCR model from {url}") from exc


def _safe_members(archive: tarfile.TarFile) -> tuple[tarfile.TarInfo, ...]:
    members = tuple(archive.getmembers())
    if len(members) > MAX_MODEL_FILES:
        raise UnsafeDocumentError("OCR model archive contains too many files")
    total = 0
    for member in members:
        normalized = member.name.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or (path.parts and ":" in path.parts[0])
            or member.issym()
            or member.islnk()
            or member.isdev()
        ):
            raise UnsafeDocumentError("OCR model archive contains an unsafe entry")
        total += member.size
        if total > MAX_MODEL_UNPACKED_BYTES:
            raise UnsafeDocumentError("OCR model archive expands beyond the configured limit")
    return members


def _extract(archive_path: Path, destination: Path, model_name: str) -> None:
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            members = _safe_members(archive)
            archive.extractall(destination, members=members, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise OcrModelNotAvailableError(f"OCR model archive for {model_name} is invalid") from exc


def _model_root(extracted: Path) -> Path:
    entries = tuple(extracted.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extracted


def _file_records(model_dir: Path) -> tuple[OcrModelFile, ...]:
    records: list[OcrModelFile] = []
    for path in sorted(item for item in model_dir.rglob("*") if item.is_file()):
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        records.append(
            OcrModelFile(
                path=path.relative_to(model_dir).as_posix(),
                size_bytes=size,
                sha256=digest.hexdigest(),
            )
        )
    if not records:
        raise OcrModelNotAvailableError("prepared OCR model directory is empty")
    return tuple(records)


def _prepare_one(target: Path, name: str) -> OcrModelRecord:
    target.mkdir(parents=True, exist_ok=True)
    url = _model_url(name)
    with tempfile.TemporaryDirectory(prefix=f".{name}-", dir=target) as temporary:
        staging = Path(temporary)
        archive_path = staging / "model.tar"
        extracted = staging / "extracted"
        extracted.mkdir()
        archive_sha256 = _download(url, archive_path)
        archive_size_bytes = archive_path.stat().st_size
        _extract(archive_path, extracted, name)
        source_root = _model_root(extracted)
        files = _file_records(source_root)
        final = target / name
        replacement = staging / "replacement"
        if source_root == extracted:
            replacement.mkdir()
            for item in tuple(extracted.iterdir()):
                shutil.move(str(item), replacement / item.name)
        else:
            source_root.rename(replacement)
        if final.exists():
            shutil.rmtree(final)
        replacement.rename(final)
    return OcrModelRecord(
        name=name,
        source_url=url,
        archive_sha256=archive_sha256,
        archive_size_bytes=archive_size_bytes,
        files=files,
    )


def prepare_ocr_models(
    target: str | os.PathLike[str] | None = None,
    *,
    profiles: Iterable[OcrProfile] = (OcrProfile.STRUCTURED, OcrProfile.TEXT),
    languages: Iterable[str] = ("az", "en", "ru"),
) -> OcrModelReport:
    """Explicitly download local models and write their deterministic checksums."""

    try:
        resolved_profiles = tuple(dict.fromkeys(OcrProfile(profile) for profile in profiles))
    except ValueError as exc:
        raise OcrConfigurationError("unknown OCR profile") from exc
    resolved_languages = tuple(
        dict.fromkeys(language.strip().lower() for language in languages if language.strip())
    )
    if not resolved_profiles:
        raise OcrModelNotAvailableError("at least one OCR profile is required")
    if not resolved_languages:
        raise OcrModelNotAvailableError("at least one OCR language is required")
    unsupported = set(resolved_languages).difference({"az", "en", "ru"})
    if unsupported:
        raise OcrConfigurationError(
            f"built-in PaddleOCR model preparation does not support: {sorted(unsupported)}"
        )
    destination = resolve_model_store(target)
    destination.mkdir(parents=True, exist_ok=True)
    records = tuple(
        sorted(
            (
                _prepare_one(destination, name)
                for name in required_model_names(resolved_profiles, resolved_languages)
            ),
            key=lambda item: item.name,
        )
    )
    manifest = OcrModelManifest(
        profiles=resolved_profiles,
        languages=resolved_languages,
        models=records,
    )
    manifest_path = destination / MODEL_MANIFEST_NAME
    temporary_manifest = destination / f".{MODEL_MANIFEST_NAME}.tmp"
    temporary_manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_manifest, manifest_path)
    return verify_ocr_models(destination)


def _load_manifest(target: Path) -> OcrModelManifest | None:
    path = target / MODEL_MANIFEST_NAME
    try:
        return OcrModelManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verify_ocr_models(
    target: str | os.PathLike[str] | None = None,
    *,
    required: Iterable[str] | None = None,
) -> OcrModelReport:
    """Verify a prepared model store without making network requests or changes."""

    destination = resolve_model_store(target)
    manifest = _load_manifest(destination)
    if manifest is None:
        return OcrModelReport(target=destination, valid=False, missing=(MODEL_MANIFEST_NAME,))
    records = {record.name: record for record in manifest.models}
    requested = tuple(sorted(set(required if required is not None else records)))
    missing: list[str] = []
    corrupted: list[str] = []
    verified: list[str] = []
    for name in requested:
        record = records.get(name)
        if record is None:
            missing.append(name)
            continue
        model_dir = destination / name
        if not model_dir.is_dir():
            missing.append(name)
            continue
        valid = True
        for file in record.files:
            path = model_dir.joinpath(*PurePosixPath(file.path).parts)
            try:
                data_size = path.stat().st_size
            except OSError:
                valid = False
                break
            if data_size != file.size_bytes:
                valid = False
                break
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            except OSError:
                valid = False
                break
            if digest.hexdigest() != file.sha256:
                valid = False
                break
        if valid:
            verified.append(name)
        else:
            corrupted.append(name)
    return OcrModelReport(
        target=destination,
        valid=not missing and not corrupted,
        verified_models=tuple(verified),
        missing=tuple(missing),
        corrupted=tuple(corrupted),
    )
