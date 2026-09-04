"""Tests for fact-preserving normalization, reconciliation, and quality."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue, ValidationError

from document_parser import (
    BoundingBox,
    ContainerBlock,
    ContainerRole,
    ContentBlock,
    CoordinateUnit,
    Diagnostic,
    DiagnosticSeverity,
    Document,
    DocumentFormat,
    DocumentMetadata,
    DocumentStatus,
    FigureBlock,
    HeadingBlock,
    ListBlock,
    ListItem,
    ListKind,
    NormalizationOptions,
    PageBreakBlock,
    ParagraphBlock,
    QualityOptions,
    QualityReport,
    QualityScope,
    QualityUnit,
    SourceInfo,
    SourceLocation,
    TableBlock,
    TableCell,
    TableRow,
    TextSpan,
    assess_quality,
    normalize_document,
)
from document_parser.normalization import _comparison_text
from document_parser.quality import apply_quality


def source_info(document_format: DocumentFormat = DocumentFormat.PDF) -> SourceInfo:
    return SourceInfo(
        name=f"sample.{document_format.value}",
        size_bytes=10,
        sha256="a" * 64,
        format=document_format,
        media_type="application/octet-stream",
    )


def point_box(x: float = 0, y: float = 0, width: float = 80, height: float = 10) -> BoundingBox:
    return BoundingBox(
        x=x,
        y=y,
        width=width,
        height=height,
        canvas_width=100,
        canvas_height=100,
        unit=CoordinateUnit.POINT,
    )


def location(*, confidence: float | None = None, box: BoundingBox | None = None) -> SourceLocation:
    return SourceLocation(page_number=1, bounding_box=box, confidence=confidence)


def document_with(
    *blocks: ContentBlock, status: DocumentStatus = DocumentStatus.COMPLETE
) -> Document:
    return Document(
        document_id=f"sha256:{'a' * 64}",
        source=source_info(),
        blocks=blocks,
        status=status,
    )


def paragraph(
    block_id: str,
    text: str,
    *,
    method: str | None = None,
    confidence: float | None = None,
    box: BoundingBox | None = None,
) -> ParagraphBlock:
    attributes: dict[str, JsonValue] = {"extraction_method": method} if method else {}
    if method == "native":
        attributes["active_for_rag"] = False
    return ParagraphBlock(
        block_id=block_id,
        spans=(TextSpan(text=text),),
        source=location(confidence=confidence, box=box),
        attributes=attributes,
    )


def page(
    *blocks: ContentBlock, scan_candidate: bool = True, ocr_applied: bool = True
) -> ContainerBlock:
    return ContainerBlock(
        block_id="page-1",
        role=ContainerRole.PAGE,
        source=SourceLocation(page_number=1),
        attributes={"scan_candidate": scan_candidate, "ocr_applied": ocr_applied},
        blocks=blocks,
    )


def test_normalization_options_are_validated_and_frozen() -> None:
    options = NormalizationOptions()
    with pytest.raises(ValidationError):
        options.enabled = False
    with pytest.raises(ValidationError):
        NormalizationOptions(duplicate_text_similarity=1.1)
    document = document_with(paragraph("p", "untouched"))
    assert normalize_document(document, options=NormalizationOptions(enabled=False)) is document


def test_text_cleanup_merges_spans_preserves_original_and_hides_empty() -> None:
    original = ParagraphBlock(
        block_id="p",
        spans=(
            TextSpan(text=" A\u0308\t", bold=True),
            TextSpan(text=" B\r\n\x00", bold=True),
        ),
    )
    empty = ParagraphBlock(block_id="empty", spans=(TextSpan(text=" \t\x01 "),))
    result = normalize_document(document_with(original, empty))
    first, second = result.blocks
    assert isinstance(first, ParagraphBlock)
    assert first.spans == (TextSpan(text="ÄB\n", bold=True),)
    assert "normalization_original_spans" in first.attributes
    assert second.attributes["active_for_rag"] is False
    assert second.attributes["normalization_decision"] == "empty_or_decorative"


def test_nested_content_titles_margins_and_heading_levels_are_normalized() -> None:
    header = ContainerBlock(
        block_id="header",
        role=ContainerRole.SECTION,
        title=(TextSpan(text=" Head\t"),),
        attributes={"story": "header", "repeated_margin": True},
        blocks=(paragraph("header-p", "Header"),),
    )
    nested_list = ListBlock(
        block_id="list",
        kind=ListKind.UNORDERED,
        items=(ListItem(blocks=(paragraph("item", " Item  text "),)),),
    )
    table = TableBlock(
        block_id="table",
        row_count=1,
        column_count=1,
        rows=(
            TableRow(
                row_index=0,
                cells=(TableCell(column_index=0, blocks=(paragraph("cell", " Cell\tvalue "),)),),
            ),
        ),
    )
    result = normalize_document(
        document_with(
            HeadingBlock(block_id="h1", level=1, spans=(TextSpan(text="One"),)),
            HeadingBlock(block_id="h4", level=4, spans=(TextSpan(text="Four"),)),
            header,
            nested_list,
            table,
            PageBreakBlock(block_id="break"),
        )
    )
    heading = result.blocks[1]
    assert isinstance(heading, HeadingBlock)
    assert heading.level == 2
    assert heading.attributes["normalization_original_heading_level"] == 4
    normalized_header = result.blocks[2]
    assert isinstance(normalized_header, ContainerBlock)
    assert normalized_header.title == (TextSpan(text="Head"),)
    assert normalized_header.attributes["active_for_rag"] is False
    assert normalized_header.blocks[0].attributes["active_for_rag"] is False
    normalized_list = result.blocks[3]
    assert isinstance(normalized_list, ListBlock)
    assert normalized_list.items[0].blocks[0].spans[0].text == "Item text"  # type: ignore[union-attr]
    normalized_table = result.blocks[4]
    assert isinstance(normalized_table, TableBlock)
    assert normalized_table.rows[0].cells[0].blocks[0].spans[0].text == "Cell value"  # type: ignore[union-attr]


def test_reconciliation_selects_ocr_duplicate_on_scan_and_native_on_force() -> None:
    box = point_box()
    ocr = paragraph("ocr", "Same text", method="ocr", confidence=0.9, box=box)
    native = paragraph("native", "same  text", method="native", box=box)

    scan_result = normalize_document(document_with(page(ocr, native)))
    scan_page = scan_result.blocks[0]
    assert isinstance(scan_page, ContainerBlock)
    assert scan_page.blocks[0].attributes["active_for_rag"] is True
    assert scan_page.blocks[1].attributes["active_for_rag"] is False
    assert scan_page.blocks[0].attributes["reconciliation_decision"] == "duplicate"
    assert scan_result.diagnostics == ()

    force_result = normalize_document(document_with(page(ocr, native, scan_candidate=False)))
    force_page = force_result.blocks[0]
    assert isinstance(force_page, ContainerBlock)
    assert force_page.blocks[0].attributes["active_for_rag"] is False
    assert force_page.blocks[1].attributes["active_for_rag"] is True


def test_reconciliation_keeps_complements_and_reports_conflicts() -> None:
    first = point_box(y=0)
    separate = point_box(y=30)
    ocr_conflict = paragraph("ocr-conflict", "OCR value", method="ocr", confidence=0.9, box=first)
    ocr_extra = paragraph("ocr-extra", "Extra", method="ocr", confidence=0.8, box=separate)
    native = paragraph("native", "Native value", method="native", box=first)
    no_box = paragraph("native-no-box", "No box", method="native")
    result = normalize_document(document_with(page(ocr_conflict, ocr_extra, native, no_box)))
    result_page = result.blocks[0]
    assert isinstance(result_page, ContainerBlock)
    assert all(block.attributes["active_for_rag"] is True for block in result_page.blocks[:2])
    assert all(block.attributes["active_for_rag"] is False for block in result_page.blocks[2:3])
    assert result_page.blocks[3].attributes["active_for_rag"] is True
    assert result.diagnostics[0].code == "reconciliation.conflict"
    assert result.diagnostics[0].details["selected_source"] == "ocr"
    assert normalize_document(result).diagnostics == result.diagnostics

    untouched = document_with(page(ocr_conflict, native, ocr_applied=False))
    assert normalize_document(untouched).diagnostics == ()
    assert (
        normalize_document(
            document_with(page(ocr_conflict, native)),
            options=NormalizationOptions(reconcile_ocr=False),
        ).diagnostics
        == ()
    )


def test_quality_models_validate_flags_schema_and_weights() -> None:
    with pytest.raises(ValidationError, match="sum to 1"):
        QualityOptions(coverage_weight=0.5)
    with pytest.raises(ValidationError, match="unique and sorted"):
        QualityUnit(
            scope=QualityScope.DOCUMENT,
            identifier="doc",
            text_characters=1,
            confidence=1,
            score=1,
            flags=("z", "a"),
        )
    unit = QualityUnit(
        scope=QualityScope.DOCUMENT,
        identifier="doc",
        text_characters=1,
        confidence=1,
        score=1,
    )
    with pytest.raises(ValidationError, match="unique and sorted"):
        QualityReport(
            overall_score=1,
            coverage_score=1,
            confidence_score=1,
            structure_score=1,
            fidelity_score=1,
            units=(unit,),
            flags=("z", "a"),
        )
    old = document_with().model_copy(update={"schema_version": "0.1"})
    assert Document.model_validate(old.model_dump()).schema_version == "0.1"
    with pytest.raises(ValidationError, match=r"schema 0\.1"):
        Document.model_validate({**old.model_dump(), "quality": assess_quality(document_with())})


def test_quality_scores_document_units_and_status_precedence() -> None:
    clean = document_with(paragraph("p", "Good", confidence=0.5))
    report = assess_quality(clean)
    assert report.coverage_score == 1
    assert report.confidence_score == 0.5
    assert report.overall_score == 0.85
    assert report.units[0].scope is QualityScope.DOCUMENT

    empty = assess_quality(document_with())
    assert empty.overall_score == 0.65
    assert empty.units[0].flags == ("quality.no_text",)

    diagnostic = Diagnostic(
        code="ocr.table_unstructured",
        message="table lost",
        severity=DiagnosticSeverity.ERROR,
    )
    scan = document_with(
        page(paragraph("inactive", "ignored", method="native")),
        status=DocumentStatus.NEEDS_REVIEW,
    ).model_copy(update={"diagnostics": (diagnostic,)})
    scored = apply_quality(scan, QualityOptions())
    assert scored.schema_version == "0.2"
    assert scored.status is DocumentStatus.NEEDS_REVIEW
    assert scored.quality is not None
    assert scored.quality.coverage_score == 0
    assert scored.quality.structure_score == 0.9
    assert scored.quality.fidelity_score == 0.75
    assert "quality.incomplete_coverage" in scored.quality.flags

    partial = apply_quality(
        document_with(paragraph("p", "text"), status=DocumentStatus.PARTIAL),
        QualityOptions(),
    )
    assert partial.status is DocumentStatus.PARTIAL
    original = document_with(paragraph("p", "text"))
    assert apply_quality(original, QualityOptions(enabled=False)) is original


def test_quality_handles_sheet_list_table_and_info_diagnostics() -> None:
    nested = ListBlock(
        block_id="list",
        kind=ListKind.ORDERED,
        start=1,
        items=(ListItem(blocks=(paragraph("item", "abc", confidence=0.0),)),),
    )
    table = TableBlock(
        block_id="table",
        row_count=1,
        column_count=2,
        rows=(
            TableRow(
                row_index=0,
                cells=(
                    TableCell(column_index=0, displayed_text="value"),
                    TableCell(column_index=1, blocks=(paragraph("nested", "nested"),)),
                ),
            ),
        ),
        source=location(confidence=0.5),
    )
    sheet = ContainerBlock(
        block_id="sheet",
        role=ContainerRole.SHEET,
        attributes={"sheet_name": "Data"},
        blocks=(nested, table),
    )
    info = Diagnostic(
        code="ocr.applied",
        message="done",
        severity=DiagnosticSeverity.INFO,
    )
    document = Document(
        document_id=f"sha256:{'a' * 64}",
        source=source_info(DocumentFormat.XLSX),
        blocks=(sheet,),
        diagnostics=(info,),
    )
    report = assess_quality(document)
    assert report.units[0].scope is QualityScope.SHEET
    assert report.units[0].identifier == "Data"
    assert report.units[0].text_characters == 14
    assert report.confidence_score == 0.6071
    assert report.fidelity_score == 1
    assert report.flags == ("quality.low_confidence",)
    completed = apply_quality(document, QualityOptions())
    assert completed.status is DocumentStatus.NEEDS_REVIEW


def test_document_metadata_date_remains_unaffected_by_normalization() -> None:
    document = document_with(paragraph("p", "text")).model_copy(
        update={"metadata": DocumentMetadata(created_at=datetime(2026, 1, 1, tzinfo=UTC))}
    )
    result = normalize_document(document)
    assert result.metadata.created_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_reconciliation_text_projection_covers_nested_and_non_text_blocks() -> None:
    container = ContainerBlock(
        block_id="container",
        role=ContainerRole.SECTION,
        blocks=(paragraph("inside", "Inside"),),
    )
    figure = FigureBlock(
        block_id="figure",
        asset_id=f"asset:sha256:{'b' * 64}",
        alt_text="Diagram",
    )
    table = TableBlock(
        block_id="table-projection",
        row_count=1,
        column_count=1,
        rows=(
            TableRow(
                row_index=0,
                cells=(TableCell(column_index=0, blocks=(paragraph("nested-p", "Nested"),)),),
            ),
        ),
    )
    listed = ListBlock(
        block_id="projected-list",
        kind=ListKind.UNORDERED,
        items=(ListItem(blocks=(paragraph("listed-p", "Listed"),)),),
    )
    assert _comparison_text(container) == "inside"
    assert _comparison_text(figure) == "diagram"
    assert _comparison_text(table) == "nested"
    assert _comparison_text(listed) == "listed"
    assert _comparison_text(PageBreakBlock(block_id="projected-break")) == ""

    untouched = ParagraphBlock(block_id="raw", spans=(TextSpan(text=" A\t B "),))
    normalized = normalize_document(
        document_with(untouched),
        options=NormalizationOptions(normalize_unicode=False, normalize_whitespace=False),
    )
    assert normalized.blocks[0].spans[0].text == " A\t B "  # type: ignore[union-attr]
