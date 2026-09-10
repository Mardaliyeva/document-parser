# ruff: noqa: RUF001
"""Tests for the optional, local-only Gradio inspection interface."""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from document_parser import (
    AssetPayload,
    AssetRef,
    BoundingBox,
    ConversionResult,
    CoordinateUnit,
    Diagnostic,
    DiagnosticSeverity,
    Document,
    DocumentFormat,
    DocumentParser,
    DocumentStatus,
    MarkdownOptions,
    OcrModelReport,
    OcrOptions,
    OcrPageInput,
    OcrPageResult,
    OcrRegion,
    OcrRegionKind,
    OcrTextLine,
    ParagraphBlock,
    QualityReport,
    QualityScope,
    QualityUnit,
    SourceInfo,
    SourceLocation,
    TextSpan,
    UiDependencyNotAvailableError,
    UnsafeDocumentError,
    cli,
    ui,
)
from document_parser.exceptions import ErrorCode
from document_parser.sources import ParseOptions

SYNTHETIC_FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


def _result(
    *,
    status: DocumentStatus = DocumentStatus.COMPLETE,
    quality: bool = True,
    assets: bool = True,
) -> ConversionResult:
    payload = b"image"
    payload_digest = hashlib.sha256(payload).hexdigest()
    image_ref = AssetRef(
        asset_id=f"asset:sha256:{payload_digest}",
        filename=f"{payload_digest}.png",
        media_type="image/png",
        sha256=payload_digest,
        size_bytes=len(payload),
    )
    text_payload = b"notes"
    text_digest = hashlib.sha256(text_payload).hexdigest()
    text_ref = AssetRef(
        asset_id=f"asset:sha256:{text_digest}",
        filename=f"{text_digest}.txt",
        media_type="text/plain",
        sha256=text_digest,
        size_bytes=len(text_payload),
    )
    source_digest = hashlib.sha256(b"source").hexdigest()
    quality_report = (
        QualityReport(
            overall_score=0.9123,
            coverage_score=1,
            confidence_score=0.9,
            structure_score=0.8,
            fidelity_score=0.95,
            units=(
                QualityUnit(
                    scope=QualityScope.DOCUMENT,
                    identifier="report.pdf",
                    text_characters=4,
                    confidence=0.9,
                    score=0.9,
                ),
            ),
        )
        if quality
        else None
    )
    refs = (image_ref, text_ref) if assets else ()
    payloads = (
        (AssetPayload(ref=image_ref, data=payload), AssetPayload(ref=text_ref, data=text_payload))
        if assets
        else ()
    )
    source = SourceInfo(
        name="report.pdf",
        size_bytes=6,
        sha256=source_digest,
        format=DocumentFormat.PDF,
        media_type="application/pdf",
    )
    document = Document(
        schema_version="0.2",
        document_id=f"sha256:{source_digest}",
        source=source,
        blocks=(ParagraphBlock(block_id="paragraph:1", spans=(TextSpan(text="text"),)),),
        assets=refs,
        status=status,
        diagnostics=(
            Diagnostic(code="sample.info", message="Info", severity=DiagnosticSeverity.INFO),
            Diagnostic(
                code="sample.warning",
                message="Warning",
                location=SourceLocation(page_number=1, block_index=0),
            ),
        ),
        quality=quality_report,
    )
    return ConversionResult(document=document, markdown="# Report\n", assets=payloads)


class _FakeParser:
    def __init__(self, result: ConversionResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[Path, Any]] = []

    def convert(
        self,
        source: str | Path,
        *,
        filename: str | None = None,
        markdown_options: Any = None,
    ) -> ConversionResult:
        self.calls.append((Path(source), markdown_options))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _UiOcrEngine:
    name = "ui-test-ocr"

    def __init__(self) -> None:
        self.pages: list[int] = []

    def recognize(self, page: OcrPageInput, _options: OcrOptions) -> OcrPageResult:
        self.pages.append(page.page_number)
        box = BoundingBox(
            x=0,
            y=0,
            width=page.width_pixels,
            height=min(100, page.height_pixels),
            canvas_width=page.width_pixels,
            canvas_height=page.height_pixels,
            unit=CoordinateUnit.PIXEL,
        )
        line = OcrTextLine(text="UI OCR nəticəsi", bounding_box=box, confidence=0.99)
        return OcrPageResult(
            page_number=page.page_number,
            engine=self.name,
            models=("ui-test-v1",),
            regions=(
                OcrRegion(
                    order=0,
                    kind=OcrRegionKind.TEXT,
                    bounding_box=box,
                    confidence=0.99,
                    lines=(line,),
                ),
            ),
        )


def test_ui_dependency_is_lazy_and_has_stable_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "gradio" not in vars(ui)
    assert ErrorCode.UI_DEPENDENCY_NOT_AVAILABLE.value == "ui_dependency_not_available"
    assert UiDependencyNotAvailableError.code is ErrorCode.UI_DEPENDENCY_NOT_AVAILABLE

    real_import = ui._import_optional

    def missing(name: str) -> Any:
        if name == "gradio":
            raise ImportError("missing")
        return real_import(name)

    monkeypatch.setattr(ui, "_import_optional", missing)
    with pytest.raises(UiDependencyNotAvailableError, match=r"document-parser\[ui\]"):
        ui._load_gradio()

    marker = object()
    monkeypatch.setattr(ui, "_import_optional", lambda _name: marker)
    assert ui._load_gradio() is marker


def test_artifact_store_lifecycle_and_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "artifacts"
    store = ui._ArtifactStore(root, ttl_seconds=10)
    first_id, first = store.create_job()
    second_id, second = store.create_job()
    assert first.parent == root
    assert second.parent == root

    monkeypatch.setattr(ui, "_last_modified", lambda path: 1.0 if path == first else 100.0)
    store.cleanup_expired(now=50.0)
    assert not first.exists()
    assert second.exists()

    store.remove_job(None)
    store.remove_job("not-registered")
    store.remove_job(second_id)
    assert not second.exists()
    store.close()
    assert root.exists()
    assert first_id != second_id

    owned = ui._ArtifactStore(ttl_seconds=1)
    owned_root = owned.root
    owned.create_job()
    owned.close()
    owned.close()
    assert not owned_root.exists()


def test_last_modified_handles_missing_path(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_text("x", encoding="utf-8")
    assert ui._last_modified(path) > 0
    path.unlink()
    assert ui._last_modified(path) == 0


def test_deterministic_zip_has_safe_sorted_fixed_entries(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    assets = bundle / "assets"
    assets.mkdir(parents=True)
    (bundle / "document.md").write_text("text\n", encoding="utf-8", newline="\n")
    (assets / "image.png").write_bytes(b"image")

    first = ui.create_deterministic_bundle_zip(bundle, tmp_path / "first.zip")
    second = ui.create_deterministic_bundle_zip(bundle, tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            "bundle/",
            "bundle/assets/",
            "bundle/assets/image.png",
            "bundle/document.md",
        ]
        assert all(info.date_time == ui.ZIP_TIMESTAMP for info in archive.infolist())
        assert archive.read("bundle/document.md") == b"text\n"


def test_zip_rejects_invalid_sources_and_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(UnsafeDocumentError, match="normal directory"):
        ui.create_deterministic_bundle_zip(missing, tmp_path / "missing.zip")

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    entry = bundle / "entry"
    entry.write_text("x", encoding="utf-8")
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == entry)
    with pytest.raises(UnsafeDocumentError, match="symbolic links"):
        ui.create_deterministic_bundle_zip(bundle, tmp_path / "bad.zip")
    monkeypatch.setattr(Path, "is_symlink", real_is_symlink)


def test_safe_zip_name_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    entry = root / "entry"
    entry.write_text("x", encoding="utf-8")
    assert ui._safe_zip_name(entry, root) == "entry"
    monkeypatch.setattr(Path, "is_symlink", lambda _path: True)
    with pytest.raises(UnsafeDocumentError, match="symbolic links"):
        ui._safe_zip_name(entry, root)

    class UnsafeRelative:
        parts = ("..", "entry")

        @staticmethod
        def is_absolute() -> bool:
            return False

    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(ui, "PurePosixPath", lambda _value: UnsafeRelative())
    with pytest.raises(UnsafeDocumentError, match="safe relative"):
        ui._safe_zip_name(entry, root)


def test_summary_and_diagnostic_helpers_cover_absent_quality() -> None:
    result = _result(quality=False, assets=False)
    summary = ui._summary(result)
    assert "Tamamlandı" in summary
    assert "Ümumi keyfiyyət:** —" in summary
    assert ui._format_score(0.5) == "0.5000"
    assert ui._diagnostic_location(result.document.diagnostics[0]) == ""
    assert '"page_number": 1' in ui._diagnostic_location(result.document.diagnostics[1])


def test_model_report_text_all_states(tmp_path: Path) -> None:
    valid = OcrModelReport(target=tmp_path, valid=True, verified_models=("a",))
    invalid = OcrModelReport(
        target=tmp_path,
        valid=False,
        missing=("a",),
        corrupted=("b",),
    )
    empty = OcrModelReport(target=tmp_path, valid=False)
    assert "1 model" in ui._model_report_text(valid)
    assert "çatışmayan: a" in ui._model_report_text(invalid)
    assert "korlanmış: b" in ui._model_report_text(invalid)
    assert ui._model_report_text(empty).endswith("hazır deyil.")


def test_controller_model_status_handles_reports_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = ui.UiController(artifact_root=tmp_path / "artifacts")
    report = OcrModelReport(target=tmp_path, valid=True, verified_models=("a", "b"))
    monkeypatch.setattr(ui, "verify_ocr_models", lambda _target: report)
    assert "2 model" in controller.model_status()

    error = UiDependencyNotAvailableError("optional missing")
    monkeypatch.setattr(ui, "verify_ocr_models", lambda _target: (_ for _ in ()).throw(error))
    assert "optional missing" in controller.model_status()
    monkeypatch.setattr(
        ui,
        "verify_ocr_models",
        lambda _target: (_ for _ in ()).throw(OSError("disk")),
    )
    assert "model store oxunmadı" in controller.model_status()
    controller.close()


def test_controller_success_maps_options_assets_downloads_and_reuses_parser(tmp_path: Path) -> None:
    parser = _FakeParser(_result(status=DocumentStatus.NEEDS_REVIEW))
    factories: list[ParseOptions] = []

    def factory(options: ParseOptions) -> _FakeParser:
        factories.append(options)
        return parser

    controller = ui.UiController(
        model_store=tmp_path / "models",
        artifact_root=tmp_path.parent / "ui-success",
        parser_factory=factory,
    )
    source = tmp_path / "upload.pdf"
    source.write_bytes(b"ignored by fake parser")
    arguments: tuple[Any, ...] = (
        source,
        "off",
        "text",
        ["AZ", "en"],
        False,
        True,
        True,
        "both",
        "html",
        None,
    )
    first = controller.convert(
        arguments[0],
        arguments[1],
        arguments[2],
        arguments[3],
        arguments[4],
        arguments[5],
        arguments[6],
        arguments[7],
        arguments[8],
        arguments[9],
        markdown_profile="fidelity",
    )
    second = controller.convert(
        arguments[0],
        arguments[1],
        arguments[2],
        arguments[3],
        arguments[4],
        arguments[5],
        arguments[6],
        arguments[7],
        arguments[8],
        str(first[0]),
        markdown_profile="fidelity",
    )

    assert len(factories) == 1
    assert factories[0].ocr.languages == ("az", "en")
    assert factories[0].ocr.model_store == (tmp_path / "models").resolve()
    markdown_options = parser.calls[0][1]
    assert markdown_options.include_source_markers is False
    assert markdown_options.include_headers_footers is True
    assert markdown_options.include_hidden_sheets is True
    assert markdown_options.formula_mode.value == "both"
    assert markdown_options.table_mode.value == "html"
    assert second[1] == "✅ Yoxlama tələb edir"
    assert "0.9123" in str(second[2])
    assert isinstance(second[5], list)
    assert len(second[5]) == 2
    assert '"schema_version":"0.2"' not in str(second[6])
    assert isinstance(second[7], list)
    assert len(second[7]) == 2
    assert isinstance(second[8], list)
    assert len(second[8]) == 1
    for output_path in second[9:12]:
        assert output_path is not None and Path(str(output_path)).is_file()
    assert not (controller.artifact_root / str(first[0])).exists()

    with zipfile.ZipFile(str(second[11])) as archive:
        names = archive.namelist()
        assert any(name.endswith("/document.md") for name in names)
        assert any(name.endswith("/document.json") for name in names)
        assert any(name.endswith("/manifest.json") for name in names)
        assert not any(name.endswith("report.pdf") for name in names)

    cleared = controller.clear_with_upload(str(second[0]))
    assert cleared == (None, *ui.UiView().component_values())
    assert not (controller.artifact_root / str(second[0])).exists()
    controller.close()


@pytest.mark.parametrize(
    "filename",
    ("rich-structure.docx", "digital-native.pdf", "structured-workbook.xlsx"),
)
def test_ui_controller_converts_each_native_synthetic_format(filename: str, tmp_path: Path) -> None:
    controller = ui.UiController(artifact_root=tmp_path.parent / f"ui-native-{filename}")
    output = controller.convert_auto(SYNTHETIC_FIXTURES / filename, None)
    assert str(output[1]).startswith("✅")
    assert Path(str(output[9])).read_text(encoding="utf-8")
    assert '"schema_version": "0.2"' in str(output[6])
    controller.clear(str(output[0]))
    controller.close()


@pytest.mark.parametrize(
    ("filename", "expected_page"),
    (
        ("scanned-image-only.pdf", 1),
        ("mixed-native-scan.pdf", 2),
        ("rotated-scan.pdf", 1),
    ),
)
def test_ui_controller_runs_injected_ocr_for_scan_corpus(
    filename: str, expected_page: int, tmp_path: Path
) -> None:
    engine = _UiOcrEngine()

    def factory(options: ParseOptions) -> DocumentParser:
        return DocumentParser(options=options, ocr_engine=engine)

    controller = ui.UiController(
        artifact_root=tmp_path.parent / f"ui-ocr-{filename}",
        parser_factory=factory,
    )
    output = controller.convert_auto(SYNTHETIC_FIXTURES / filename, None)
    assert engine.pages == [expected_page]
    assert "UI OCR nəticəsi" in str(output[4])
    assert str(output[1]).startswith("✅")
    controller.clear(str(output[0]))
    controller.close()


@pytest.mark.parametrize(
    ("source_name", "expected_code"),
    (("encrypted-negative.pdf", "invalid_document"), ("unsupported.txt", "unsupported_format")),
)
def test_ui_controller_safely_reports_invalid_inputs(
    source_name: str, expected_code: str, tmp_path: Path
) -> None:
    source = SYNTHETIC_FIXTURES / source_name
    if source_name == "unsupported.txt":
        source = tmp_path / source_name
        source.write_bytes(b"not a supported document")
    controller = ui.UiController(artifact_root=tmp_path.parent / f"ui-error-{source_name}")
    output = controller.convert(
        source,
        "off",
        "structured",
        ["az"],
        False,
        False,
        False,
        "displayed",
        "auto",
        None,
    )
    assert expected_code in str(output[1])
    assert output[0] is None
    controller.close()


def test_failed_reconversion_clears_stale_result(tmp_path: Path) -> None:
    parser = _FakeParser(_result(assets=False))
    controller = ui.UiController(
        artifact_root=tmp_path.parent / "ui-stale",
        parser_factory=lambda _options: parser,
    )
    source = tmp_path / "upload.pdf"
    source.write_bytes(b"x")
    arguments = (
        source,
        "off",
        "structured",
        ["az"],
        False,
        False,
        False,
        "displayed",
        "auto",
    )
    first = controller.convert(*arguments, None)
    first_job = controller.artifact_root / str(first[0])
    parser.result = UiDependencyNotAvailableError("missing")
    failed = controller.convert(*arguments, str(first[0]))
    assert not first_job.exists()
    assert failed[0] is None
    assert "ui_dependency_not_available" in str(failed[1])
    assert failed[2:] == UiViewEmpty
    controller.close()


UiViewEmpty = ui.UiView().component_values()[2:]


def test_controller_parser_cache_changes_with_configuration(tmp_path: Path) -> None:
    created: list[_FakeParser] = []

    def factory(_options: ParseOptions) -> _FakeParser:
        parser = _FakeParser(_result(assets=False))
        created.append(parser)
        return parser

    controller = ui.UiController(artifact_root=tmp_path / "artifacts", parser_factory=factory)
    source = tmp_path / "upload.pdf"
    source.write_bytes(b"x")
    first = controller.convert(
        source, "off", "structured", ["az"], False, False, False, "displayed", "auto", None
    )
    second = controller.convert(
        source,
        "force",
        "structured",
        ["az"],
        False,
        False,
        False,
        "displayed",
        "auto",
        str(first[0]),
    )
    assert len(created) == 2
    controller.clear(str(second[0]))
    controller.close()
    controller.close()
    with pytest.raises(RuntimeError, match="closed"):
        controller._get_parser(ParseOptions())


def test_default_parser_factory_builds_document_parser() -> None:
    parser = ui._default_parser_factory(ParseOptions())
    assert isinstance(parser, DocumentParser)
    assert parser.supported_formats == ("docx", "pdf", "xlsx")


def test_controller_empty_input_and_failure_mapping(tmp_path: Path) -> None:
    controller = ui.UiController(artifact_root=tmp_path / "empty")
    empty = controller.convert(
        None, "off", "structured", ["az"], False, False, False, "displayed", "auto", None
    )
    assert "Əvvəlcə" in str(empty[1])
    assert empty[0] is None
    controller.close()

    source = tmp_path / "upload.pdf"
    source.write_bytes(b"x")
    cases: tuple[tuple[Exception, str], ...] = (
        (UiDependencyNotAvailableError("missing"), "ui_dependency_not_available"),
        (OSError("private path"), "output_write_error"),
        (ValueError("bad value"), "invalid_configuration"),
        (RuntimeError("secret traceback"), "internal_error"),
    )
    for index, (error, code) in enumerate(cases):

        def factory(_options: ParseOptions, _error: Exception = error) -> _FakeParser:
            return _FakeParser(_error)

        failing = ui.UiController(
            artifact_root=tmp_path / f"failure-{index}",
            parser_factory=factory,
        )
        output = failing.convert(
            source,
            "off",
            "structured",
            ["az"],
            False,
            False,
            False,
            "displayed",
            "auto",
            None,
        )
        assert code in str(output[1])
        assert "private path" not in str(output[1])
        assert all(value in (None, "", [], ()) for value in output[2:])
        failing.close()


def test_controller_write_failure_removes_partial_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = ui.UiController(artifact_root=tmp_path / "artifacts")
    monkeypatch.setattr(
        ui,
        "write_conversion_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")),
    )
    with pytest.raises(OSError, match="disk"):
        controller._write_result(_result(assets=False))
    assert not tuple(controller.artifact_root.iterdir())
    controller.close()


def test_build_ui_smoke_has_private_events_and_expected_components(tmp_path: Path) -> None:
    app = ui.build_ui(artifact_root=tmp_path / "artifacts")
    try:
        config = app.config
        component_types = {component["type"] for component in config["components"]}
        assert not {"radio", "dropdown", "checkbox", "checkboxgroup"} & component_types
        technical = [item for item in config["components"] if item["type"] == "accordion"]
        assert len(technical) == 1
        assert technical[0]["props"]["label"] == "Texniki məlumatlar"
        assert technical[0]["props"]["open"] is False
        assert {"file", "markdown", "dataframe", "gallery", "downloadbutton"}.issubset(
            component_types
        )
        assert config["analytics_enabled"] is False
        assert all(
            dependency.get("api_visibility") != "public"
            for dependency in config["dependencies"]
            if dependency.get("targets")
        )
    finally:
        app._document_parser_controller.close()


def test_launch_ui_forces_local_private_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    controller = SimpleNamespace(close_calls=0)

    def close() -> None:
        controller.close_calls += 1

    controller.close = close
    app = SimpleNamespace(_document_parser_controller=controller)
    app.launch = lambda **kwargs: calls.append(kwargs)
    monkeypatch.setattr(ui, "build_ui", lambda **_kwargs: app)
    ui.launch_ui(model_store=Path("models"), port=9000, inbrowser=False)
    assert calls == [
        {
            "server_name": "127.0.0.1",
            "server_port": 9000,
            "share": False,
            "inbrowser": False,
            "strict_cors": True,
            "max_file_size": "100mb",
            "footer_links": [],
            "run_history": False,
        }
    ]
    assert controller.close_calls == 1

    app.launch = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("server"))
    with pytest.raises(RuntimeError, match="server"):
        ui.launch_ui()
    assert controller.close_calls == 2


def test_cli_ui_arguments_and_lazy_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli._port("7860") == 7860
    with pytest.raises(SystemExit):
        cli.main(("ui", "--port", "zero"))
    with pytest.raises(SystemExit):
        cli.main(("ui", "--port", "70000"))

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ui, "launch_ui", lambda **kwargs: calls.append(kwargs))
    assert cli.main(("ui", "--model-store", "models", "--port", "9000", "--no-browser")) == 0
    assert calls == [{"model_store": Path("models"), "port": 9000, "inbrowser": False}]


def test_base_import_does_not_load_gradio() -> None:
    import subprocess

    script = "import sys; import document_parser; assert 'gradio' not in sys.modules"
    subprocess.run([sys.executable, "-c", script], check=True)


def test_automatic_ui_policy_and_library_defaults(tmp_path: Path) -> None:
    created: list[ParseOptions] = []
    parser = _FakeParser(_result(assets=False))

    def factory(options: ParseOptions) -> _FakeParser:
        created.append(options)
        return parser

    controller = ui.UiController(artifact_root=tmp_path, parser_factory=factory)
    try:
        result = controller.convert_auto(Path("example.pdf"), None)
        assert str(result[1]).startswith("✅")
        assert created[0].ocr.mode.value == "auto"
        assert created[0].ocr.profile.value == "structured"
        assert created[0].ocr.languages == ("az", "en", "ru")
        assert parser.calls[0][1] == MarkdownOptions(include_source_markers=False)
        assert ParseOptions().ocr.mode.value == "off"
    finally:
        controller.close()
