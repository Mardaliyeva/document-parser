"""Tests for deterministic bundle output, batch isolation, and the CLI."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from document_parser import (
    AssetPayload,
    AssetRef,
    BatchItemReport,
    BatchItemStatus,
    BatchOptions,
    BatchReport,
    ConversionResult,
    Document,
    DocumentFormat,
    DocumentStatus,
    OcrModelReport,
    ParagraphBlock,
    QualityReport,
    QualityScope,
    QualityUnit,
    SourceInfo,
    TextSpan,
    UnsafeDocumentError,
    batch,
    cli,
    collect_input_paths,
    convert_batch,
    write_conversion_bundle,
)
from document_parser.exceptions import SourceReadError


def conversion_result(
    name: str = "Sample report.pdf",
    *,
    status: DocumentStatus = DocumentStatus.COMPLETE,
    with_asset: bool = True,
) -> ConversionResult:
    digest = hashlib.sha256(name.encode()).hexdigest()
    source = SourceInfo(
        name=name,
        size_bytes=10,
        sha256=digest,
        format=DocumentFormat.PDF,
        media_type="application/pdf",
    )
    quality = QualityReport(
        overall_score=0.9,
        coverage_score=1,
        confidence_score=1,
        structure_score=1,
        fidelity_score=1,
        units=(
            QualityUnit(
                scope=QualityScope.DOCUMENT,
                identifier=name,
                text_characters=4,
                confidence=1,
                score=1,
            ),
        ),
    )
    assets: tuple[AssetRef, ...] = ()
    payloads: tuple[AssetPayload, ...] = ()
    if with_asset:
        data = b"image"
        asset_digest = hashlib.sha256(data).hexdigest()
        ref = AssetRef(
            asset_id=f"asset:sha256:{asset_digest}",
            filename=f"{asset_digest}.png",
            media_type="image/png",
            sha256=asset_digest,
            size_bytes=len(data),
        )
        assets = (ref,)
        payloads = (AssetPayload(ref=ref, data=data),)
    document = Document(
        document_id=f"sha256:{digest}",
        source=source,
        blocks=(ParagraphBlock(block_id="p", spans=(TextSpan(text="text"),)),),
        assets=assets,
        status=status,
        quality=quality,
    )
    return ConversionResult(document=document, markdown="text\n", assets=payloads)


def test_collect_input_paths_filters_sorts_recurses_and_keeps_explicit_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "inputs"
    nested = root / "nested"
    nested.mkdir(parents=True)
    first = root / "B.PDF"
    second = nested / "a.docx"
    ignored = root / "notes.txt"
    for path in (first, second, ignored):
        path.write_bytes(b"x")

    assert collect_input_paths((root,)) == (first,)
    assert collect_input_paths((root,), recursive=True) == (first, second)
    assert collect_input_paths((ignored, ignored)) == (ignored,)


def test_write_conversion_bundle_is_complete_and_replace_is_controlled(tmp_path: Path) -> None:
    result = conversion_result()
    destination = write_conversion_bundle(result, tmp_path)
    assert destination.name.startswith("Sample_report-")
    assert (destination / "document.md").read_text(encoding="utf-8") == "text\n"
    assert '"schema_version": "0.2"' in (destination / "document.json").read_text()
    assert '"quality"' in (destination / "manifest.json").read_text()
    assert next(iter((destination / "assets").iterdir())).read_bytes() == b"image"
    assert not tuple(tmp_path.glob(".document-parser-*"))

    with pytest.raises(FileExistsError, match="already exists"):
        write_conversion_bundle(result, tmp_path)
    (destination / "obsolete").write_text("old")
    replaced = write_conversion_bundle(result, tmp_path, overwrite=True)
    assert not (replaced / "obsolete").exists()
    assert not (tmp_path / f".{destination.name}.backup").exists()


def test_bundle_rejects_unsafe_output_shapes_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = conversion_result(with_asset=False)
    output = tmp_path / "output"
    output.mkdir()
    target = output / f"Sample_report-{result.document.source.sha256[:12]}"
    target.write_text("not a directory")
    with pytest.raises(UnsafeDocumentError, match="normal directory"):
        write_conversion_bundle(result, output, overwrite=True)
    target.unlink()
    target.mkdir()
    backup = output / f".{target.name}.backup"
    backup.mkdir()
    with pytest.raises(UnsafeDocumentError, match="stale"):
        write_conversion_bundle(result, output, overwrite=True)
    backup.rmdir()

    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: True if self == output else real_is_symlink(self),
    )
    with pytest.raises(UnsafeDocumentError, match="symlink"):
        write_conversion_bundle(result, output)

    ordinary_file = tmp_path / "not-a-directory"
    ordinary_file.write_text("file")
    with pytest.raises(UnsafeDocumentError, match="normal directory"):
        write_conversion_bundle(result, ordinary_file)


def test_bundle_rejects_escaping_asset_and_rolls_back_failed_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = conversion_result()
    payload = result.assets[0]
    unsafe_ref = payload.ref.model_copy(update={"filename": "../escape.png"})
    unsafe_payload = AssetPayload.model_construct(ref=unsafe_ref, data=payload.data)
    unsafe_result = result.model_copy(update={"assets": (unsafe_payload,)})
    with pytest.raises(UnsafeDocumentError, match="escaped"):
        write_conversion_bundle(unsafe_result, tmp_path / "assets-out")

    root = tmp_path / "rollback"
    root.mkdir()
    staging = root / "staging"
    target = root / "target"
    staging.mkdir()
    target.mkdir()
    real_rename = Path.rename

    def rename(path: Path, destination: Path) -> Path:
        if path == staging:
            raise OSError("rename failed")
        return real_rename(path, destination)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError, match="rename failed"):
        batch._replace_directory(staging, target, overwrite=True)
    assert target.exists()
    assert not (root / ".target.backup").exists()


class FakeParser:
    def __init__(self, *, options: object) -> None:
        self.options = options

    def convert(self, source: Path, *, markdown_options: object) -> ConversionResult:
        if "unsafe" in source.name:
            raise UnsafeDocumentError("unsafe", source_name=source.name)
        if source.name.startswith("read"):
            raise SourceReadError("unreadable", source_name=source.name)
        if source.name.startswith("explode"):
            raise RuntimeError("private")
        return conversion_result(source.name, with_asset=False)


def test_convert_batch_sequential_parallel_and_fail_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(batch, "DocumentParser", FakeParser)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    good = inputs / "good.pdf"
    other = inputs / "other.docx"
    unsafe = inputs / "unsafe.pdf"
    for path in (good, other, unsafe):
        path.write_bytes(b"x")

    report = convert_batch((inputs,), tmp_path / "out", batch_options=BatchOptions())
    assert [item.status for item in report.items] == [
        BatchItemStatus.COMPLETE,
        BatchItemStatus.COMPLETE,
        BatchItemStatus.FAILED,
    ]
    assert report.items[-1].error_code == "unsafe_document"
    assert report.succeeded is False
    assert (tmp_path / "out" / "batch-report.json").exists()

    parallel = convert_batch(
        (good, other),
        tmp_path / "parallel",
        batch_options=BatchOptions(jobs=2),
    )
    assert parallel.succeeded is True

    failed_fast = convert_batch(
        (inputs / "a-unsafe.pdf", other),
        tmp_path / "fast",
        batch_options=BatchOptions(fail_fast=True),
    )
    assert len(failed_fast.items) == 1
    completed_fast = convert_batch(
        (good,),
        tmp_path / "fast-good",
        batch_options=BatchOptions(fail_fast=True),
    )
    assert completed_fast.succeeded
    with pytest.raises(ValueError, match="no input"):
        convert_batch((), tmp_path / "none")


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (FileExistsError("exists"), "output_exists"),
        (OSError("disk"), "output_write_error"),
        (RuntimeError("private"), "internal_error"),
    ],
)
def test_batch_isolates_output_and_unexpected_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    code: str,
) -> None:
    source = tmp_path / "good.pdf"
    source.write_bytes(b"x")
    monkeypatch.setattr(batch, "DocumentParser", FakeParser)
    monkeypatch.setattr(
        batch, "write_conversion_bundle", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure)
    )
    report = convert_batch((source,), tmp_path / code)
    assert report.items[0].error_code == code
    assert "private" not in (report.items[0].error_message or "")


def test_batch_rejects_input_symlink_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "linked.pdf"
    source.write_bytes(b"x")
    monkeypatch.setattr(batch, "DocumentParser", FakeParser)
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: True if self == source else real_is_symlink(self),
    )
    report = convert_batch((source,), tmp_path / "out")
    assert report.items[0].error_code == "unsafe_document"


def test_cli_argument_helpers_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._positive_jobs("2") == 2
    assert cli.main(("version",)) == 0
    assert capsys.readouterr().out.strip()
    with pytest.raises(SystemExit) as invalid_jobs:
        cli.main(("convert", "x.pdf", "--output", "out", "--jobs", "many"))
    assert invalid_jobs.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(("convert", "x.pdf", "--output", "out", "--jobs", "33"))
    with pytest.raises(SystemExit):
        cli.main(("convert", "x.pdf", "--output", "out", "--languages", ","))


def test_cli_inspect_models_and_error_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n%%EOF\n")
    assert cli.main(("inspect", str(source))) == 0
    assert '"format": "pdf"' in capsys.readouterr().out

    valid = OcrModelReport(target=tmp_path, valid=True)
    invalid = OcrModelReport(target=tmp_path, valid=False, missing=("manifest",))
    captured: dict[str, object] = {}

    def prepare(target: Path, *, profiles: object, languages: object) -> OcrModelReport:
        captured.update(target=target, profiles=profiles, languages=languages)
        return valid

    monkeypatch.setattr(cli, "prepare_ocr_models", prepare)
    monkeypatch.setattr(cli, "verify_ocr_models", lambda _target: invalid)
    assert (
        cli.main(
            (
                "models",
                "prepare",
                "--target",
                str(tmp_path),
                "--profiles",
                "structured,text",
                "--languages",
                "az,ru",
            )
        )
        == 0
    )
    assert len(captured["profiles"]) == 2  # type: ignore[arg-type]
    assert cli.main(("models", "verify", "--target", str(tmp_path))) == 1
    capsys.readouterr()

    monkeypatch.setattr(cli, "_run_convert", lambda _args: (_ for _ in ()).throw(ValueError("bad")))
    assert cli.main(("convert", "x.pdf", "--output", str(tmp_path))) == 2
    monkeypatch.setattr(
        cli,
        "_run_convert",
        lambda _args: (_ for _ in ()).throw(UnsafeDocumentError("unsafe")),
    )
    assert cli.main(("convert", "x.pdf", "--output", str(tmp_path))) == 3
    monkeypatch.setattr(
        cli,
        "_run_convert",
        lambda _args: (_ for _ in ()).throw(SourceReadError("missing")),
    )
    assert cli.main(("convert", "x.pdf", "--output", str(tmp_path))) == 1
    monkeypatch.setattr(cli, "_run_convert", lambda _args: (_ for _ in ()).throw(OSError("disk")))
    assert cli.main(("convert", "x.pdf", "--output", str(tmp_path))) == 1


@pytest.mark.parametrize(
    ("report", "fail_on_review", "expected"),
    [
        (
            BatchReport(items=(BatchItemReport(source="a", status=BatchItemStatus.COMPLETE),)),
            False,
            0,
        ),
        (
            BatchReport(
                items=(
                    BatchItemReport(
                        source="a",
                        status=BatchItemStatus.FAILED,
                        error_code="output_exists",
                    ),
                )
            ),
            False,
            1,
        ),
        (
            BatchReport(items=(BatchItemReport(source="a", status=BatchItemStatus.NEEDS_REVIEW),)),
            True,
            1,
        ),
        (
            BatchReport(
                items=(
                    BatchItemReport(
                        source="a",
                        status=BatchItemStatus.FAILED,
                        error_code="unsafe_document",
                    ),
                )
            ),
            False,
            3,
        ),
    ],
)
def test_cli_convert_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report: BatchReport,
    fail_on_review: bool,
    expected: int,
) -> None:
    monkeypatch.setattr(cli, "convert_batch", lambda *_args, **_kwargs: report)
    arguments = ["convert", "a.pdf", "--output", str(tmp_path)]
    if fail_on_review:
        arguments.append("--fail-on-review")
    assert cli.main(arguments) == expected


def test_cli_converts_a_real_native_pdf_into_a_full_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from reportlab.pdfgen.canvas import Canvas

    source = tmp_path / "native.pdf"
    canvas = Canvas(str(source), invariant=1)
    canvas.drawString(72, 720, "Native PDF content")
    canvas.save()
    output = tmp_path / "converted"

    assert cli.main(("convert", str(source), "--output", str(output))) == 0
    bundle = next(path for path in output.iterdir() if path.is_dir())
    assert "Native PDF content" in (bundle / "document.md").read_text(encoding="utf-8")
    assert '"schema_version": "0.2"' in (bundle / "document.json").read_text()
    assert '"quality"' in (bundle / "manifest.json").read_text()
    assert '"status": "complete"' in capsys.readouterr().out
