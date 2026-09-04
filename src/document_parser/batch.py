"""Deterministic batch conversion and atomic bundle writing."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from document_parser.exceptions import DocumentParserError, UnsafeDocumentError
from document_parser.markdown import MarkdownOptions
from document_parser.models import FrozenModel
from document_parser.parser import DocumentParser
from document_parser.results import ConversionResult
from document_parser.sources import ParseOptions

_SUPPORTED_SUFFIXES = {".docx", ".pdf", ".xlsx"}
_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")


class BatchItemStatus(StrEnum):
    """Serializable outcome for one batch input."""

    COMPLETE = "complete"
    NEEDS_REVIEW = "needs_review"
    PARTIAL = "partial"
    FAILED = "failed"


class BatchOptions(FrozenModel):
    """File discovery, concurrency, and output replacement behavior."""

    recursive: bool = False
    jobs: int = Field(default=1, ge=1, le=32)
    overwrite: bool = False
    fail_fast: bool = False


class BatchItemReport(FrozenModel):
    """Stable report entry for one attempted source."""

    source: str = Field(min_length=1)
    status: BatchItemStatus
    document_id: str | None = None
    output_directory: str | None = None
    quality_score: float | None = Field(default=None, ge=0, le=1)
    diagnostic_codes: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class BatchReport(FrozenModel):
    """Machine-readable result for an entire batch run."""

    schema_version: str = "0.1"
    items: tuple[BatchItemReport, ...]

    @property
    def succeeded(self) -> bool:
        """Whether every source produced a conversion bundle."""

        return all(item.status is not BatchItemStatus.FAILED for item in self.items)


def collect_input_paths(
    inputs: Iterable[str | os.PathLike[str]], *, recursive: bool = False
) -> tuple[Path, ...]:
    """Expand file and directory arguments without following directory symlinks."""

    discovered: dict[str, Path] = {}
    for raw in inputs:
        path = Path(raw)
        if path.is_dir() and not path.is_symlink():
            candidates = path.rglob("*") if recursive else path.iterdir()
            for candidate in candidates:
                if (
                    candidate.is_file()
                    and not candidate.is_symlink()
                    and candidate.suffix.lower() in _SUPPORTED_SUFFIXES
                ):
                    discovered[str(candidate.resolve())] = candidate
        else:
            discovered[str(path.resolve())] = path
    return tuple(discovered[key] for key in sorted(discovered, key=str.casefold))


def _safe_output_root(output_directory: str | os.PathLike[str]) -> Path:
    root = Path(output_directory)
    absolute = root.absolute()
    existing = tuple(path for path in (absolute, *absolute.parents) if path.exists())
    if any(path.is_symlink() for path in existing):
        raise UnsafeDocumentError("output path cannot contain a symlink")
    if root.exists() and not root.is_dir():
        raise UnsafeDocumentError("output path must be a normal directory")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _bundle_name(result: ConversionResult) -> str:
    stem = Path(result.document.source.name).stem
    safe = _SAFE_STEM.sub("_", stem).strip("._-") or "document"
    return f"{safe}-{result.document.source.sha256[:12]}"


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _manifest(result: ConversionResult) -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "document_id": result.document.document_id,
        "source": result.document.source.model_dump(mode="json"),
        "status": result.document.status.value,
        "quality": (
            result.document.quality.model_dump(mode="json")
            if result.document.quality is not None
            else None
        ),
        "diagnostics": [item.model_dump(mode="json") for item in result.document.diagnostics],
        "assets": [item.model_dump(mode="json") for item in result.document.assets],
    }


def _replace_directory(staging: Path, target: Path, *, overwrite: bool) -> None:
    if not target.exists():
        staging.rename(target)
        return
    if target.is_symlink() or not target.is_dir():
        raise UnsafeDocumentError("bundle target must be a normal directory")
    if not overwrite:
        raise FileExistsError(f"output bundle already exists: {target.name}")
    backup = target.parent / f".{target.name}.backup"
    if backup.exists():
        raise UnsafeDocumentError("stale bundle backup prevents safe replacement")
    target.rename(backup)
    try:
        staging.rename(target)
    except Exception:
        backup.rename(target)
        raise
    shutil.rmtree(backup)


def write_conversion_bundle(
    result: ConversionResult,
    output_directory: str | os.PathLike[str],
    *,
    overwrite: bool = False,
) -> Path:
    """Write one complete bundle through a same-filesystem staging directory."""

    root = _safe_output_root(output_directory)
    target = root / _bundle_name(result)
    staging = Path(tempfile.mkdtemp(prefix=".document-parser-", dir=root))
    try:
        (staging / "assets").mkdir()
        (staging / "document.md").write_text(result.markdown, encoding="utf-8", newline="\n")
        (staging / "document.json").write_text(
            result.document.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        (staging / "manifest.json").write_text(
            _json_text(_manifest(result)), encoding="utf-8", newline="\n"
        )
        for asset in result.assets:
            destination = staging / "assets" / asset.ref.filename
            if destination.parent != staging / "assets":
                raise UnsafeDocumentError("asset filename escaped the bundle directory")
            destination.write_bytes(asset.data)
        _replace_directory(staging, target, overwrite=overwrite)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def _convert_one(
    source: Path,
    output: Path,
    parse_options: ParseOptions,
    markdown_options: MarkdownOptions | None,
    batch_options: BatchOptions,
) -> BatchItemReport:
    source_label = source.as_posix()
    try:
        if source.is_symlink():
            raise UnsafeDocumentError("input file cannot be a symlink", source_name=source.name)
        result = DocumentParser(options=parse_options).convert(
            source, markdown_options=markdown_options
        )
        destination = write_conversion_bundle(result, output, overwrite=batch_options.overwrite)
        return BatchItemReport(
            source=source_label,
            status=BatchItemStatus(result.document.status.value),
            document_id=result.document.document_id,
            output_directory=destination.name,
            quality_score=(
                result.document.quality.overall_score
                if result.document.quality is not None
                else None
            ),
            diagnostic_codes=tuple(sorted({item.code for item in result.document.diagnostics})),
        )
    except DocumentParserError as exc:
        return BatchItemReport(
            source=source_label,
            status=BatchItemStatus.FAILED,
            error_code=exc.code.value,
            error_message=str(exc),
        )
    except FileExistsError as exc:
        return BatchItemReport(
            source=source_label,
            status=BatchItemStatus.FAILED,
            error_code="output_exists",
            error_message=str(exc),
        )
    except OSError:
        return BatchItemReport(
            source=source_label,
            status=BatchItemStatus.FAILED,
            error_code="output_write_error",
            error_message="the conversion bundle could not be written",
        )
    except Exception:
        return BatchItemReport(
            source=source_label,
            status=BatchItemStatus.FAILED,
            error_code="internal_error",
            error_message="an unexpected conversion failure occurred",
        )


def _write_batch_report(report: BatchReport, output: Path) -> None:
    temporary = output / ".batch-report.json.tmp"
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, output / "batch-report.json")


def convert_batch(
    inputs: Iterable[str | os.PathLike[str]],
    output_directory: str | os.PathLike[str],
    *,
    parse_options: ParseOptions | None = None,
    markdown_options: MarkdownOptions | None = None,
    batch_options: BatchOptions | None = None,
) -> BatchReport:
    """Convert files or directories and always write a deterministic batch report."""

    resolved_parse = parse_options or ParseOptions()
    resolved_batch = batch_options or BatchOptions()
    output = _safe_output_root(output_directory)
    sources = collect_input_paths(inputs, recursive=resolved_batch.recursive)
    if not sources:
        raise ValueError("no input documents were found")
    if resolved_batch.fail_fast:
        items: list[BatchItemReport] = []
        for source in sources:
            item = _convert_one(source, output, resolved_parse, markdown_options, resolved_batch)
            items.append(item)
            if item.status is BatchItemStatus.FAILED:
                break
    elif resolved_batch.jobs == 1:
        items = [
            _convert_one(source, output, resolved_parse, markdown_options, resolved_batch)
            for source in sources
        ]
    else:
        with ThreadPoolExecutor(max_workers=resolved_batch.jobs) as executor:
            items = list(
                executor.map(
                    lambda source: _convert_one(
                        source,
                        output,
                        resolved_parse,
                        markdown_options,
                        resolved_batch,
                    ),
                    sources,
                )
            )
    report = BatchReport(items=tuple(items))
    _write_batch_report(report, output)
    return report
