"""End-to-end regression tests backed by representative binary fixtures."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from document_parser import (
    BatchItemStatus,
    BatchOptions,
    BoundingBox,
    ContainerBlock,
    CoordinateUnit,
    DocumentFormat,
    DocumentParser,
    DocumentStatus,
    FigureBlock,
    HeadingBlock,
    InvalidDocumentError,
    ListBlock,
    OcrMode,
    OcrOptions,
    OcrPageInput,
    OcrPageResult,
    OcrRegion,
    OcrRegionKind,
    OcrTable,
    OcrTableCell,
    OcrTextLine,
    PageBreakBlock,
    ParagraphBlock,
    ParseOptions,
    TableBlock,
    TextSpan,
    convert,
    convert_batch,
)
from document_parser.models import ContentBlock

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


def _walk(blocks: tuple[ContentBlock, ...]) -> Iterator[ContentBlock]:
    for block in blocks:
        yield block
        if isinstance(block, ContainerBlock):
            yield from _walk(block.blocks)
        elif isinstance(block, ListBlock):
            for item in block.items:
                yield from _walk(item.blocks)
        elif isinstance(block, TableBlock):
            for row in block.rows:
                for cell in row.cells:
                    yield from _walk(cell.blocks)


def _box(
    page: OcrPageInput,
    x: float,
    y: float,
    width: float,
    height: float,
) -> BoundingBox:
    return BoundingBox(
        x=x,
        y=y,
        width=width,
        height=height,
        canvas_width=page.width_pixels,
        canvas_height=page.height_pixels,
        unit=CoordinateUnit.PIXEL,
    )


class SyntheticOcrEngine:
    """Deterministic engine that exercises PDF rendering and IR mapping in CI."""

    name = "synthetic-ocr"

    def __init__(self) -> None:
        self.pages: list[OcrPageInput] = []

    def recognize(self, page: OcrPageInput, _options: OcrOptions) -> OcrPageResult:
        self.pages.append(page)
        width = float(page.width_pixels)
        height = float(page.height_pixels)
        title_box = _box(page, width * 0.06, height * 0.04, width * 0.70, height * 0.08)
        body_box = _box(page, width * 0.06, height * 0.14, width * 0.84, height * 0.10)
        list_box = _box(page, width * 0.06, height * 0.25, width * 0.50, height * 0.10)
        table_box = _box(page, width * 0.06, height * 0.38, width * 0.84, height * 0.34)
        lines = (
            OcrTextLine(
                text="1. Birinci maddə",
                bounding_box=_box(page, width * 0.07, height * 0.26, width * 0.35, height * 0.035),
                confidence=0.98,
                language="az",
            ),
            OcrTextLine(
                text="2. İkinci maddə",
                bounding_box=_box(page, width * 0.07, height * 0.31, width * 0.35, height * 0.035),
                confidence=0.98,
                language="az",
            ),
        )
        table = OcrTable(
            row_count=2,
            column_count=2,
            cells=(
                OcrTableCell(row_index=0, column_index=0, text="Məhsul", confidence=0.99),
                OcrTableCell(row_index=0, column_index=1, text="Say", confidence=0.99),
                OcrTableCell(row_index=1, column_index=0, text="Telefon", confidence=0.98),
                OcrTableCell(row_index=1, column_index=1, text="12", confidence=0.99),
            ),
        )
        return OcrPageResult(
            page_number=page.page_number,
            engine=self.name,
            models=("synthetic-v1",),
            regions=(
                OcrRegion(
                    order=0,
                    kind=OcrRegionKind.DOCUMENT_TITLE,
                    bounding_box=title_box,
                    confidence=0.99,
                    lines=(
                        OcrTextLine(
                            text="Skan sənəd nümunəsi",
                            bounding_box=title_box,
                            confidence=0.99,
                            language="az",
                        ),
                    ),
                ),
                OcrRegion(
                    order=1,
                    kind=OcrRegionKind.TEXT,
                    bounding_box=body_box,
                    confidence=0.98,
                    lines=(
                        OcrTextLine(
                            text="Azərbaycan English Русский",
                            bounding_box=body_box,
                            confidence=0.98,
                        ),
                    ),
                ),
                OcrRegion(
                    order=2,
                    kind=OcrRegionKind.LIST,
                    bounding_box=list_box,
                    confidence=0.98,
                    lines=lines,
                ),
                OcrRegion(
                    order=3,
                    kind=OcrRegionKind.TABLE,
                    bounding_box=table_box,
                    confidence=0.98,
                    table=table,
                ),
            ),
        )


def _ocr_options() -> ParseOptions:
    return ParseOptions(
        ocr=OcrOptions(
            mode=OcrMode.AUTO,
            dpi=72,
            max_page_pixels=1_000_000,
            max_total_pixels=2_000_000,
        )
    )


def test_fixture_manifest_matches_binary_payloads() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "0.1"
    records = manifest["fixtures"]
    assert len(records) == 7
    for record in records:
        path = FIXTURES / record["filename"]
        payload = path.read_bytes()
        assert len(payload) == record["size_bytes"]
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]


def test_rich_docx_exercises_structure_assets_and_normalization() -> None:
    result = convert(FIXTURES / "rich-structure.docx")
    blocks = tuple(_walk(result.document.blocks))

    assert result.document.source.format is DocumentFormat.DOCX
    assert result.document.status is DocumentStatus.COMPLETE
    assert result.document.metadata.title == "Universal Parser Synthetic Document"
    assert result.document.quality is not None
    assert result.document.quality.overall_score == 1.0
    assert any(isinstance(block, ListBlock) for block in blocks)
    assert any(isinstance(block, TableBlock) for block in blocks)
    assert any(isinstance(block, FigureBlock) for block in blocks)
    assert any(isinstance(block, PageBreakBlock) for block in blocks)
    assert any(
        isinstance(block, HeadingBlock)
        and "".join(span.text for span in block.spans) == "Heading level jump"
        and block.level == 2
        and block.attributes["normalization_original_heading_level"] == 3
        for block in blocks
    )
    assert any(
        isinstance(block, ParagraphBlock)
        and any(isinstance(span, TextSpan) and span.href for span in block.spans)
        for block in blocks
    )
    assert len(result.assets) == 1
    assert "Repeated header" not in result.markdown
    assert "This sentence is hidden" not in result.markdown
    assert 'rowspan="2"' in result.markdown
    assert 'colspan="2"' in result.markdown


def test_xlsx_exercises_formulas_types_regions_merges_and_images() -> None:
    result = convert(FIXTURES / "structured-workbook.xlsx")
    blocks = tuple(_walk(result.document.blocks))
    sheets = tuple(block for block in result.document.blocks if isinstance(block, ContainerBlock))
    tables = tuple(block for block in blocks if isinstance(block, TableBlock))

    assert result.document.source.format is DocumentFormat.XLSX
    assert result.document.status is DocumentStatus.COMPLETE
    assert ["".join(span.text for span in sheet.title) for sheet in sheets] == [
        "Sales Data",
        "Reference",
    ]
    assert any(
        cell.formula == "=B6*C6" for table in tables for row in table.rows for cell in row.cells
    )
    assert any(
        cell.row_span == 2 and cell.column_span == 2
        for table in tables
        for row in table.rows
        for cell in row.cells
    )
    assert len(result.assets) == 1
    assert "2026-01-15T00:00:00" in result.markdown
    assert "Azərbaycan dili" in result.markdown
    assert "Русский текст" in result.markdown


def test_digital_pdf_stays_native_and_skips_auto_ocr() -> None:
    engine = SyntheticOcrEngine()
    result = DocumentParser(options=_ocr_options(), ocr_engine=engine).convert(
        FIXTURES / "digital-native.pdf"
    )
    blocks = tuple(_walk(result.document.blocks))

    assert result.document.source.format is DocumentFormat.PDF
    assert result.document.status is DocumentStatus.COMPLETE
    assert engine.pages == []
    assert not any(item.code == "pdf.ocr_required" for item in result.document.diagnostics)
    assert "Azərbaycan sənədi" in result.markdown
    assert any(
        isinstance(block, TableBlock)
        and any(cell.displayed_text == "Product" for row in block.rows for cell in row.cells)
        for block in blocks
    )


@pytest.mark.xfail(
    strict=True,
    reason="PDF text-alignment fallback currently misclassifies two-column prose as a table",
)
def test_digital_pdf_two_column_prose_is_not_a_table() -> None:
    result = convert(FIXTURES / "digital-native.pdf")
    second_page = result.document.blocks[1]
    assert isinstance(second_page, ContainerBlock)
    assert not any(isinstance(block, TableBlock) for block in second_page.blocks)
    assert "Left column begins here." in result.markdown
    assert "Right column begins here." in result.markdown


@pytest.mark.xfail(
    strict=True,
    reason="DOCX inline serializer currently strips significant spaces between adjacent spans",
)
def test_docx_markdown_preserves_spaces_between_formatted_spans() -> None:
    markdown = convert(FIXTURES / "rich-structure.docx").markdown
    assert "**Purpose** This file" in markdown
    assert "**Bold text**, *italic text*, <u>underlined text</u>" in markdown


@pytest.mark.xfail(
    strict=True,
    reason="DOCX metadata title and visible Title paragraph are currently emitted twice",
)
def test_docx_title_is_not_duplicated_in_markdown() -> None:
    markdown = convert(FIXTURES / "rich-structure.docx").markdown
    assert markdown.count("Universal Parser Synthetic Document") == 1


@pytest.mark.parametrize(
    ("filename", "expected_pages"),
    (("scanned-image-only.pdf", 1), ("mixed-native-scan.pdf", 2), ("rotated-scan.pdf", 1)),
)
def test_scan_fixtures_are_detected_without_ocr(filename: str, expected_pages: int) -> None:
    result = convert(FIXTURES / filename)
    pages = tuple(block for block in result.document.blocks if isinstance(block, ContainerBlock))
    candidates = tuple(page for page in pages if page.attributes.get("scan_candidate") is True)

    assert result.document.status is DocumentStatus.NEEDS_REVIEW
    assert len(pages) == expected_pages
    assert len(candidates) == 1
    assert any(item.code == "pdf.ocr_required" for item in result.document.diagnostics)


@pytest.mark.parametrize(
    ("filename", "page_number", "rotation"),
    (
        ("scanned-image-only.pdf", 1, 0),
        ("mixed-native-scan.pdf", 2, 0),
        ("rotated-scan.pdf", 1, 90),
    ),
)
def test_scan_fixtures_complete_with_deterministic_injected_ocr(
    filename: str, page_number: int, rotation: int
) -> None:
    engine = SyntheticOcrEngine()
    parser = DocumentParser(options=_ocr_options(), ocr_engine=engine)
    result = parser.convert(FIXTURES / filename)
    blocks = tuple(_walk(result.document.blocks))

    assert [(page.page_number, page.rotation) for page in engine.pages] == [(page_number, rotation)]
    assert result.document.status is DocumentStatus.COMPLETE
    assert result.document.quality is not None
    assert result.document.quality.overall_score >= 0.99
    assert not any(item.code == "pdf.ocr_required" for item in result.document.diagnostics)
    assert any(item.code == "ocr.applied" for item in result.document.diagnostics)
    assert "Skan sənəd nümunəsi" in result.markdown
    assert "Azərbaycan English Русский" in result.markdown
    assert any(isinstance(block, ListBlock) for block in blocks)
    assert any(isinstance(block, TableBlock) for block in blocks)
    if filename == "mixed-native-scan.pdf":
        first_page = result.document.blocks[0]
        assert isinstance(first_page, ContainerBlock)
        assert any(
            block.attributes.get("active_for_rag") is not False for block in first_page.blocks
        )


def test_encrypted_pdf_is_rejected() -> None:
    with pytest.raises(InvalidDocumentError, match="encrypted PDF"):
        convert(FIXTURES / "encrypted-negative.pdf")


def test_full_corpus_batch_isolates_the_expected_negative_case(tmp_path: Path) -> None:
    report = convert_batch(
        (FIXTURES,),
        tmp_path / "output",
        batch_options=BatchOptions(jobs=2),
    )
    names = tuple(Path(item.source).name for item in report.items)
    failures = tuple(item for item in report.items if item.status is BatchItemStatus.FAILED)

    assert names == tuple(sorted(names, key=str.casefold))
    assert len(report.items) == 7
    assert len(failures) == 1
    assert Path(failures[0].source).name == "encrypted-negative.pdf"
    assert failures[0].error_code == "invalid_document"
    assert (tmp_path / "output" / "batch-report.json").is_file()
