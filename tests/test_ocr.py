"""Tests for engine-neutral OCR contracts and PDF post-processing."""

from __future__ import annotations

import hashlib
import io
import sys
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from document_parser import (
    AdapterInput,
    AdapterOutput,
    AssetPayload,
    BoundingBox,
    ContainerBlock,
    ContainerRole,
    CoordinateUnit,
    Diagnostic,
    DiagnosticSeverity,
    Document,
    DocumentFormat,
    DocumentParser,
    DocumentStatus,
    FigureBlock,
    HeadingBlock,
    ListBlock,
    ListItem,
    ListKind,
    OcrConfigurationError,
    OcrDependencyNotAvailableError,
    OcrEngineDiagnostic,
    OcrMode,
    OcrModelNotAvailableError,
    OcrOptions,
    OcrPageInput,
    OcrPageResult,
    OcrRegion,
    OcrRegionKind,
    OcrTable,
    OcrTableCell,
    OcrTextLine,
    ParagraphBlock,
    ParseOptions,
    SourceInfo,
    SourceLocation,
    TableBlock,
    TableCell,
    TableRow,
    TextSpan,
    UnsafeDocumentError,
    to_markdown,
)
from document_parser.exceptions import OcrExecutionError
from document_parser.ocr import (
    _deactivate_native,
    _is_page_background,
    _render_page,
    _select_pages,
    apply_pdf_ocr,
)

SOURCE_HASH = "a" * 64


def pixel_box(x: float = 0, y: float = 0, width: float = 20, height: float = 10) -> BoundingBox:
    return BoundingBox(
        x=x,
        y=y,
        width=width,
        height=height,
        canvas_width=100,
        canvas_height=100,
        unit=CoordinateUnit.PIXEL,
    )


def page_input(number: int = 1, *, width: int = 100, height: int = 100) -> OcrPageInput:
    stream = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(stream, format="PNG")
    return OcrPageInput(
        page_number=number,
        image_png=stream.getvalue(),
        width_pixels=width,
        height_pixels=height,
        source_width_points=50,
        source_height_points=50,
        dpi=300,
    )


def source_info() -> SourceInfo:
    return SourceInfo(
        name="scan.pdf",
        size_bytes=14,
        sha256=SOURCE_HASH,
        format=DocumentFormat.PDF,
        media_type="application/pdf",
    )


def document_output(*, pages: int = 1, candidate: bool = True) -> AdapterOutput:
    blocks = tuple(
        ContainerBlock(
            block_id=f"page-{number}",
            role=ContainerRole.PAGE,
            source=SourceLocation(page_number=number),
            attributes={"scan_candidate": candidate, "rotation": 0},
            blocks=(
                ParagraphBlock(
                    block_id=f"native-{number}",
                    spans=(TextSpan(text="native shadow"),),
                ),
            ),
        )
        for number in range(1, pages + 1)
    )
    diagnostics = tuple(
        Diagnostic(
            code="pdf.ocr_required",
            message="OCR required",
            location=SourceLocation(page_number=number),
        )
        for number in range(1, pages + 1)
        if candidate
    )
    document = Document(
        document_id=f"sha256:{SOURCE_HASH}",
        source=source_info(),
        blocks=blocks,
        status=DocumentStatus.NEEDS_REVIEW if candidate else DocumentStatus.COMPLETE,
        diagnostics=diagnostics,
    )
    return AdapterOutput(document=document)


class FakeEngine:
    name = "fake"

    def __init__(self, result: OcrPageResult | Exception) -> None:
        self.result = result
        self.pages: list[OcrPageInput] = []

    def recognize(self, page: OcrPageInput, options: OcrOptions) -> OcrPageResult:
        self.pages.append(page)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result.model_copy(update={"page_number": page.page_number})


class FakePdf:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def install_fake_renderer(
    monkeypatch: pytest.MonkeyPatch, rendered: OcrPageInput | None = None
) -> None:
    monkeypatch.setitem(
        sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=lambda _data: FakePdf())
    )
    monkeypatch.setattr(
        "document_parser.ocr._render_page",
        lambda _pdf, number, _rotation, _options: (rendered or page_input(number)).model_copy(
            update={"page_number": number}
        ),
    )


def successful_result() -> OcrPageResult:
    lines = (OcrTextLine(text="OCR title", bounding_box=pixel_box(), confidence=0.99),)
    table = OcrTable(
        row_count=2,
        column_count=3,
        cells=(
            OcrTableCell(
                row_index=0,
                column_index=0,
                text="A",
                bounding_box=pixel_box(0, 20, 50, 20),
                confidence=0.9,
            ),
            OcrTableCell(
                row_index=0,
                column_index=1,
                text="B",
                bounding_box=pixel_box(50, 20, 50, 20),
                confidence=0.9,
            ),
            OcrTableCell(
                row_index=1,
                column_index=0,
                column_span=2,
                text="Merged",
                bounding_box=pixel_box(0, 40, 100, 20),
                confidence=0.9,
            ),
            OcrTableCell(
                row_index=1,
                column_index=2,
                text="",
                bounding_box=pixel_box(80, 40, 20, 20),
            ),
        ),
    )
    return OcrPageResult(
        page_number=1,
        engine="fake",
        models=("model-a",),
        regions=(
            OcrRegion(
                order=0,
                kind=OcrRegionKind.DOCUMENT_TITLE,
                bounding_box=pixel_box(),
                confidence=0.99,
                lines=lines,
            ),
            OcrRegion(
                order=1,
                kind=OcrRegionKind.LIST,
                bounding_box=pixel_box(0, 10, 50, 10),
                confidence=0.95,
                lines=(
                    OcrTextLine(text="2. First", bounding_box=pixel_box(0, 10), confidence=0.95),
                    OcrTextLine(text="3. Second", bounding_box=pixel_box(0, 15), confidence=0.95),
                ),
            ),
            OcrRegion(
                order=2,
                kind=OcrRegionKind.TABLE,
                bounding_box=pixel_box(0, 20, 100, 40),
                confidence=0.9,
                table=table,
            ),
            OcrRegion(
                order=3,
                kind=OcrRegionKind.FIGURE,
                bounding_box=pixel_box(0, 70, 20, 20),
                confidence=0.9,
            ),
            OcrRegion(
                order=4,
                kind=OcrRegionKind.TEXT,
                bounding_box=pixel_box(0, 90, 50, 10),
                confidence=0.98,
                lines=(
                    OcrTextLine(
                        text="Body text",
                        bounding_box=pixel_box(0, 90, 50, 10),
                        confidence=0.98,
                    ),
                ),
            ),
        ),
    )


def test_ocr_models_validate_json_and_cross_field_rules() -> None:
    options = OcrOptions(languages=(" AZ ", "EN"), max_page_pixels=10, max_total_pixels=20)
    assert options.languages == ("az", "en")
    with pytest.raises(ValidationError, match="duplicates"):
        OcrOptions(languages=("az", "AZ"))
    with pytest.raises(ValidationError, match="language codes"):
        OcrOptions(languages=())
    with pytest.raises(ValidationError, match="max_page_pixels"):
        OcrOptions(max_page_pixels=20, max_total_pixels=10)

    restored = OcrPageInput.model_validate_json(page_input().model_dump_json())
    assert restored == page_input()
    with pytest.raises(ValidationError, match="multiple of 90"):
        page_input().model_copy(update={"rotation": 1}).model_dump()
        OcrPageInput(**{**page_input().model_dump(), "rotation": 1})


def test_ocr_region_and_table_validation() -> None:
    point_box = pixel_box().model_copy(update={"unit": CoordinateUnit.POINT})
    with pytest.raises(ValidationError, match="pixel coordinates"):
        OcrTextLine(text="x", bounding_box=point_box, confidence=1)
    with pytest.raises(ValidationError, match="table data"):
        OcrRegion(
            order=0,
            kind=OcrRegionKind.TABLE,
            bounding_box=pixel_box(),
            confidence=1,
        )
    with pytest.raises(ValidationError, match="only table"):
        OcrRegion(
            order=0,
            kind=OcrRegionKind.TEXT,
            bounding_box=pixel_box(),
            confidence=1,
            lines=(OcrTextLine(text="x", bounding_box=pixel_box(), confidence=1),),
            table=OcrTable(row_count=1, column_count=1, cells=()),
        )
    with pytest.raises(ValidationError, match="at least one line"):
        OcrRegion(
            order=0,
            kind=OcrRegionKind.TEXT,
            bounding_box=pixel_box(),
            confidence=1,
        )
    with pytest.raises(ValidationError, match="unique increasing"):
        OcrPageResult(
            page_number=1,
            engine="x",
            regions=(
                OcrRegion(
                    order=1,
                    kind=OcrRegionKind.FIGURE,
                    bounding_box=pixel_box(),
                    confidence=1,
                ),
                OcrRegion(
                    order=1,
                    kind=OcrRegionKind.FIGURE,
                    bounding_box=pixel_box(),
                    confidence=1,
                ),
            ),
        )

    with pytest.raises(ValidationError, match="unique ordered positions"):
        OcrTable(
            row_count=1,
            column_count=1,
            cells=(
                OcrTableCell(row_index=0, column_index=0),
                OcrTableCell(row_index=0, column_index=0),
            ),
        )
    with pytest.raises(ValidationError, match="row_count"):
        OcrTable(
            row_count=1,
            column_count=1,
            cells=(OcrTableCell(row_index=0, column_index=0, row_span=2),),
        )
    with pytest.raises(ValidationError, match="column_count"):
        OcrTable(
            row_count=1,
            column_count=1,
            cells=(OcrTableCell(row_index=0, column_index=0, column_span=2),),
        )
    with pytest.raises(ValidationError, match="cannot overlap"):
        OcrTable(
            row_count=1,
            column_count=2,
            cells=(
                OcrTableCell(row_index=0, column_index=0, column_span=2),
                OcrTableCell(row_index=0, column_index=1),
            ),
        )


def test_successful_selective_ocr_maps_ir_and_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_renderer(monkeypatch)
    options = ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO))
    engine = FakeEngine(successful_result())
    source = AdapterInput(source_info(), io.BytesIO(b"%PDF-1.7\nEOF"))

    result, reused = apply_pdf_ocr(source, document_output(), options, engine)

    assert reused is engine
    assert result.document.status is DocumentStatus.COMPLETE
    page = result.document.blocks[0]
    assert isinstance(page, ContainerBlock)
    assert page.attributes["ocr_applied"] is True
    assert page.attributes["text_source"] == "ocr"
    assert isinstance(page.blocks[0], HeadingBlock)
    assert isinstance(page.blocks[1], ListBlock)
    assert isinstance(page.blocks[2], TableBlock)
    assert page.blocks[-1].attributes["active_for_rag"] is False
    assert page.blocks[0].source is not None
    assert page.blocks[0].source.bounding_box is not None
    assert page.blocks[0].source.bounding_box.unit is CoordinateUnit.POINT
    assert not any(item.code == "pdf.ocr_required" for item in result.document.diagnostics)
    assert any(item.code == "ocr.applied" for item in result.document.diagnostics)
    markdown = to_markdown(result.document)
    assert "OCR title" in markdown
    assert "2. First" in markdown
    assert "Merged" in markdown
    assert "Body text" in markdown
    assert "native shadow" not in markdown


def test_review_and_failure_outcomes_are_page_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_renderer(monkeypatch)
    line = OcrTextLine(text="uncertain", bounding_box=pixel_box(), confidence=0.1)
    result = OcrPageResult(
        page_number=1,
        engine="fake",
        ambiguous_language=True,
        diagnostics=(
            OcrEngineDiagnostic(
                code="ocr.table_unstructured",
                message="table fallback",
                severity=DiagnosticSeverity.WARNING,
            ),
        ),
        regions=(
            OcrRegion(
                order=0,
                kind=OcrRegionKind.PARAGRAPH_TITLE,
                bounding_box=pixel_box(),
                confidence=0.1,
                lines=(line,),
            ),
        ),
    )
    source = AdapterInput(source_info(), io.BytesIO(b"%PDF-1.7\nEOF"))
    reviewed, _ = apply_pdf_ocr(
        source,
        document_output(),
        ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
        FakeEngine(result),
    )
    assert reviewed.document.status is DocumentStatus.NEEDS_REVIEW
    assert {item.code for item in reviewed.document.diagnostics} >= {
        "ocr.low_confidence",
        "ocr.language_ambiguous",
        "ocr.table_unstructured",
    }

    failed, _ = apply_pdf_ocr(
        source,
        document_output(),
        ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
        FakeEngine(OcrExecutionError("failed")),
    )
    assert failed.document.status is DocumentStatus.PARTIAL
    assert failed.document.diagnostics[-1].code == "ocr.page_failed"


def test_no_text_force_mode_and_noop_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_renderer(monkeypatch)
    empty = OcrPageResult(page_number=1, engine="fake")
    source = AdapterInput(source_info(), io.BytesIO(b"%PDF-1.7\nEOF"))
    result, _ = apply_pdf_ocr(
        source,
        document_output(candidate=False),
        ParseOptions(ocr=OcrOptions(mode=OcrMode.FORCE)),
        FakeEngine(empty),
    )
    assert result.document.status is DocumentStatus.NEEDS_REVIEW
    assert {item.code for item in result.document.diagnostics} >= {
        "ocr.no_text_detected",
        "ocr.low_confidence",
    }
    fallback_page = result.document.blocks[0]
    assert isinstance(fallback_page, ContainerBlock)
    assert fallback_page.attributes["text_source"] == "native_fallback"
    assert fallback_page.blocks[0].attributes.get("active_for_rag") is not False
    assert "native shadow" in to_markdown(result.document)

    original = document_output(candidate=False)
    off, engine = apply_pdf_ocr(source, original, ParseOptions(), None)
    assert off is original and engine is None
    auto, engine = apply_pdf_ocr(
        source,
        original,
        ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
        None,
    )
    assert auto is original and engine is None


def test_ocr_limits_and_invalid_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_renderer(monkeypatch)
    source = AdapterInput(source_info(), io.BytesIO(b"%PDF-1.7\nEOF"))
    with pytest.raises(UnsafeDocumentError, match="max_pages"):
        apply_pdf_ocr(
            source,
            document_output(pages=2),
            ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO, max_pages=1)),
            FakeEngine(successful_result()),
        )
    with pytest.raises(OcrConfigurationError, match="protocol"):
        apply_pdf_ocr(
            source,
            document_output(),
            ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
            object(),  # type: ignore[arg-type]
        )

    rendered = page_input(width=20, height=20)
    install_fake_renderer(monkeypatch, rendered)
    with pytest.raises(UnsafeDocumentError, match="max_total_pixels"):
        apply_pdf_ocr(
            source,
            document_output(pages=2),
            ParseOptions(
                ocr=OcrOptions(
                    mode=OcrMode.AUTO,
                    max_pages=2,
                    max_page_pixels=500,
                    max_total_pixels=500,
                )
            ),
            FakeEngine(successful_result()),
        )


@pytest.mark.parametrize(
    "failure",
    [
        OcrDependencyNotAvailableError("dependency"),
        OcrModelNotAvailableError("model"),
        OcrConfigurationError("configuration"),
    ],
)
def test_global_ocr_failures_are_not_downgraded(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    install_fake_renderer(monkeypatch)
    source = AdapterInput(source_info(), io.BytesIO(b"%PDF-1.7\nEOF"))
    with pytest.raises(type(failure), match=str(failure)):
        apply_pdf_ocr(
            source,
            document_output(),
            ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
            FakeEngine(failure),
        )


def test_renderer_dependency_open_and_wrong_page_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = AdapterInput(source_info(), io.BytesIO(b"%PDF-1.7\nEOF"))
    options = ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO))
    engine = FakeEngine(successful_result())

    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    with pytest.raises(OcrDependencyNotAvailableError, match="rendering requires"):
        apply_pdf_ocr(source, document_output(), options, engine)

    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        SimpleNamespace(PdfDocument=lambda _data: (_ for _ in ()).throw(ValueError("bad"))),
    )
    with pytest.raises(OcrExecutionError, match="could not be opened"):
        apply_pdf_ocr(source, document_output(), options, engine)

    install_fake_renderer(monkeypatch)
    wrong = successful_result().model_copy(update={"page_number": 2})

    class WrongPageEngine:
        name = "wrong"

        def recognize(self, _page: OcrPageInput, _options: OcrOptions) -> OcrPageResult:
            return wrong

    failed, _ = apply_pdf_ocr(source, document_output(), options, WrongPageEngine())
    assert failed.document.status is DocumentStatus.PARTIAL
    assert failed.document.diagnostics[-1].code == "ocr.page_failed"


def test_default_engine_is_lazy_and_parser_convert_uses_injected_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_renderer(monkeypatch)
    created = FakeEngine(successful_result())
    monkeypatch.setattr(
        "document_parser.paddle_ocr.PaddleOcrEngine",
        lambda *_args, **_kwargs: created,
    )
    source = AdapterInput(source_info(), io.BytesIO(b"%PDF-1.7\nEOF"))
    result, reused = apply_pdf_ocr(
        source,
        document_output(),
        ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
    )
    assert reused is created
    assert result.document.status is DocumentStatus.COMPLETE

    class PdfAdapter:
        format = DocumentFormat.PDF

        def parse(self, prepared: AdapterInput, _options: ParseOptions) -> AdapterOutput:
            base = document_output().document
            return AdapterOutput(
                document=base.model_copy(
                    update={
                        "document_id": f"sha256:{prepared.info.sha256}",
                        "source": prepared.info,
                    }
                )
            )

    parser = DocumentParser(
        options=ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
        adapters=(PdfAdapter(),),
        ocr_engine=FakeEngine(successful_result()),
    )
    converted = parser.convert(b"%PDF-1.7\nEOF", filename="scan.pdf")
    assert converted.document.status is DocumentStatus.COMPLETE
    assert "OCR title" in converted.markdown


def test_page_selection_and_background_helpers_cover_conservative_paths() -> None:
    assert _select_pages(document_output().document, OcrOptions(mode=OcrMode.OFF)) == ()
    no_source = FigureBlock(block_id="figure", asset_id="asset")
    assert not _is_page_background(no_source)


def test_native_deactivation_recurses_through_all_container_shapes() -> None:
    paragraph = ParagraphBlock(block_id="nested", spans=(TextSpan(text="text"),))
    listing = ListBlock(
        block_id="list",
        kind=ListKind.UNORDERED,
        items=(ListItem(blocks=(paragraph,)),),
    )
    table = TableBlock(
        block_id="table",
        row_count=1,
        column_count=1,
        rows=(
            TableRow(
                row_index=0,
                cells=(
                    TableCell(
                        column_index=0, blocks=(paragraph.model_copy(update={"block_id": "cell"}),)
                    ),
                ),
            ),
        ),
    )
    container = ContainerBlock(
        block_id="container", role=ContainerRole.SECTION, blocks=(listing, table)
    )

    hidden = _deactivate_native(container)

    assert isinstance(hidden, ContainerBlock)
    hidden_list = hidden.blocks[0]
    hidden_table = hidden.blocks[1]
    assert isinstance(hidden_list, ListBlock)
    assert isinstance(hidden_table, TableBlock)
    assert hidden.attributes["active_for_rag"] is False
    assert hidden_list.items[0].blocks[0].attributes["active_for_rag"] is False
    assert hidden_table.rows[0].cells[0].blocks[0].attributes["active_for_rag"] is False


class RenderBitmap:
    def __init__(self) -> None:
        self.closed = False

    def to_pil(self) -> Image.Image:
        return Image.new("RGBA", (100, 50), (0, 0, 0, 128))

    def close(self) -> None:
        self.closed = True


class RenderPage:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False
        self.bitmap = RenderBitmap()

    def get_size(self) -> tuple[int, int]:
        return (24, 12)

    def render(self, **_kwargs: object) -> RenderBitmap:
        if self.fail:
            raise ValueError("render failed")
        return self.bitmap

    def close(self) -> None:
        self.closed = True


def test_pdf_page_rendering_flattens_alpha_closes_resources_and_limits() -> None:
    page = RenderPage()
    rendered = _render_page([page], 1, 450, OcrOptions())
    assert rendered.width_pixels == 100
    assert rendered.height_pixels == 50
    assert rendered.rotation == 90
    assert page.closed and page.bitmap.closed

    limited = RenderPage()
    with pytest.raises(UnsafeDocumentError, match="max_page_pixels"):
        _render_page(
            [limited],
            1,
            0,
            OcrOptions(max_page_pixels=100, max_total_pixels=100),
        )
    assert limited.closed

    broken = RenderPage(fail=True)
    with pytest.raises(OcrExecutionError, match="could not be rendered"):
        _render_page([broken], 1, 0, OcrOptions())
    assert broken.closed


def test_pdf_document_without_close_method_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        SimpleNamespace(PdfDocument=lambda _data: [RenderPage()]),
    )
    source = AdapterInput(source_info(), io.BytesIO(b"%PDF-1.7\nEOF"))
    result, _ = apply_pdf_ocr(
        source,
        document_output(),
        ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
        FakeEngine(successful_result()),
    )
    assert result.document.status is DocumentStatus.COMPLETE


def test_real_pdf_adapter_and_pdfium_renderer_feed_custom_engine() -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    image_stream = io.BytesIO()
    Image.new("RGB", (612, 792), "white").save(image_stream, format="PNG")
    pdf_stream = io.BytesIO()
    canvas = Canvas(pdf_stream, pagesize=letter, invariant=1)
    canvas.drawImage(ImageReader(io.BytesIO(image_stream.getvalue())), 0, 0, 612, 792)
    canvas.save()

    class RenderAwareEngine:
        name = "render-aware"

        def __init__(self) -> None:
            self.page: OcrPageInput | None = None

        def recognize(self, page: OcrPageInput, _options: OcrOptions) -> OcrPageResult:
            self.page = page
            box = BoundingBox(
                x=0,
                y=0,
                width=page.width_pixels,
                height=max(1, page.height_pixels // 10),
                canvas_width=page.width_pixels,
                canvas_height=page.height_pixels,
                unit=CoordinateUnit.PIXEL,
            )
            line = OcrTextLine(text="Rendered scan", bounding_box=box, confidence=0.99)
            return OcrPageResult(
                page_number=page.page_number,
                engine=self.name,
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

    engine = RenderAwareEngine()
    parser = DocumentParser(
        options=ParseOptions(
            ocr=OcrOptions(
                mode=OcrMode.AUTO,
                dpi=72,
                max_page_pixels=1_000_000,
                max_total_pixels=1_000_000,
            )
        ),
        ocr_engine=engine,
    )
    result = parser.convert(pdf_stream.getvalue(), filename="scan.pdf")

    assert engine.page is not None
    assert (engine.page.width_pixels, engine.page.height_pixels) == (612, 792)
    assert result.document.status is DocumentStatus.COMPLETE
    assert "Rendered scan" in result.markdown


def test_background_figure_is_hidden_after_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_renderer(monkeypatch)
    output = document_output()
    page = output.document.blocks[0]
    assert isinstance(page, ContainerBlock)
    figure = FigureBlock(
        block_id="background",
        asset_id="asset-1",
        source=SourceLocation(
            page_number=1,
            asset_id="asset-1",
            bounding_box=BoundingBox(
                x=0,
                y=0,
                width=80,
                height=80,
                canvas_width=100,
                canvas_height=100,
                unit=CoordinateUnit.POINT,
            ),
        ),
    )
    from document_parser import AssetRef

    asset = AssetRef(
        asset_id="asset-1",
        filename="image.png",
        media_type="image/png",
        sha256=hashlib.sha256(b"").hexdigest(),
        size_bytes=0,
    )
    updated_document = output.document.model_copy(
        update={
            "blocks": (page.model_copy(update={"blocks": (*page.blocks, figure)}),),
            "assets": (asset,),
        }
    )
    source = AdapterInput(source_info(), io.BytesIO(b"%PDF-1.7\nEOF"))
    result, _ = apply_pdf_ocr(
        source,
        AdapterOutput(document=updated_document, assets=(AssetPayload(ref=asset, data=b""),)),
        ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
        FakeEngine(successful_result()),
    )
    final_page = result.document.blocks[0]
    assert isinstance(final_page, ContainerBlock)
    background = next(block for block in final_page.blocks if block.block_id == "background")
    assert background.attributes["active_for_rag"] is False
