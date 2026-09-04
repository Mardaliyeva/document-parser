"""Validation tests for the format-independent document IR."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from document_parser import (
    AssetRef,
    BoundingBox,
    ContainerBlock,
    ContainerRole,
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
    PageBreakBlock,
    ParagraphBlock,
    SourceInfo,
    SourceLocation,
    TableBlock,
    TableCell,
    TableRow,
    TextSpan,
)

SOURCE_HASH = "a" * 64
ASSET_HASH = "b" * 64


def make_source_info() -> SourceInfo:
    return SourceInfo(
        name="sample.pdf",
        size_bytes=42,
        sha256=SOURCE_HASH,
        format=DocumentFormat.PDF,
        media_type="application/pdf",
        supplied_extension=".pdf",
        extension_matches=True,
    )


def paragraph(block_id: str, text: str = "text") -> ParagraphBlock:
    return ParagraphBlock(block_id=block_id, spans=(TextSpan(text=text),))


def make_full_document() -> Document:
    page_location = SourceLocation(
        page_number=1,
        block_index=0,
        bounding_box=BoundingBox(
            x=10,
            y=20,
            width=100,
            height=30,
            canvas_width=612,
            canvas_height=792,
            unit=CoordinateUnit.POINT,
        ),
        confidence=0.98,
    )
    image_location = SourceLocation(
        asset_id="asset-1",
        bounding_box=BoundingBox(
            x=0,
            y=0,
            width=100,
            height=50,
            canvas_width=100,
            canvas_height=50,
            unit=CoordinateUnit.PIXEL,
        ),
    )
    nested_list = ListBlock(
        block_id="list-inner",
        kind=ListKind.UNORDERED,
        items=(ListItem(blocks=(paragraph("p-list-inner", "Nested"),)),),
    )
    outer_list = ListBlock(
        block_id="list-outer",
        kind=ListKind.ORDERED,
        start=3,
        items=(ListItem(blocks=(paragraph("p-list", "Item"), nested_list)),),
    )
    table = TableBlock(
        block_id="table-1",
        row_count=2,
        column_count=2,
        rows=(
            TableRow(
                row_index=0,
                cells=(
                    TableCell(
                        column_index=0,
                        row_span=2,
                        is_header=True,
                        raw_value="Name",
                        displayed_text="Name",
                        blocks=(paragraph("p-cell", "Name"),),
                    ),
                    TableCell(
                        column_index=1,
                        is_header=True,
                        raw_value=2026,
                        displayed_text="2026",
                        formula="=YEAR(TODAY())",
                    ),
                ),
            ),
            TableRow(
                row_index=1,
                cells=(
                    TableCell(
                        column_index=1,
                        raw_value=True,
                        displayed_text="TRUE",
                    ),
                ),
            ),
        ),
    )
    container = ContainerBlock(
        block_id="page-1",
        role=ContainerRole.PAGE,
        title=(TextSpan(text="Page 1"),),
        source=page_location,
        attributes={"rotation": 0},
        blocks=(
            HeadingBlock(
                block_id="heading-1",
                level=1,
                spans=(
                    TextSpan(
                        text="Title",
                        bold=True,
                        italic=True,
                        underline=True,
                        strikethrough=True,
                        code=True,
                        href="https://example.test",
                    ),
                ),
            ),
            paragraph("p-main", "Body"),
            outer_list,
            table,
            FigureBlock(
                block_id="figure-1",
                asset_id="asset-1",
                caption=(TextSpan(text="Diagram"),),
                alt_text="A diagram",
                source=image_location,
            ),
            PageBreakBlock(block_id="break-1"),
        ),
    )
    return Document(
        document_id=f"sha256:{SOURCE_HASH}",
        source=make_source_info(),
        metadata=DocumentMetadata(
            title="Sample",
            authors=("Author",),
            subject="Testing",
            keywords=("IR", "RAG"),
            language="az",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            modified_at=datetime(2026, 1, 2, tzinfo=UTC),
            custom={"revision": 2, "flags": [True, None]},
        ),
        blocks=(container,),
        assets=(
            AssetRef(
                asset_id="asset-1",
                filename="image.png",
                media_type="image/png",
                sha256=ASSET_HASH,
                size_bytes=10,
            ),
        ),
        status=DocumentStatus.NEEDS_REVIEW,
        diagnostics=(
            Diagnostic(
                code="ocr.low_confidence",
                message="Review this region.",
                severity=DiagnosticSeverity.WARNING,
                location=page_location,
                details={"score": 0.51},
            ),
        ),
    )


def test_full_document_json_round_trip_and_frozen_contract() -> None:
    document = make_full_document()

    restored = Document.model_validate_json(document.model_dump_json())
    schema = Document.model_json_schema()

    assert restored == document
    assert restored.schema_version == "0.2"
    assert restored.blocks[0].type == "container"
    assert schema["properties"]["schema_version"]["enum"] == ["0.1", "0.2"]
    assert schema["properties"]["schema_version"]["default"] == "0.2"
    field_name = "status"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(document, field_name, DocumentStatus.COMPLETE)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "x": 90,
                "y": 0,
                "width": 20,
                "height": 10,
                "canvas_width": 100,
                "canvas_height": 100,
                "unit": "pixel",
            },
            "canvas width",
        ),
        (
            {
                "x": 0,
                "y": 95,
                "width": 10,
                "height": 10,
                "canvas_width": 100,
                "canvas_height": 100,
                "unit": "pixel",
            },
            "canvas height",
        ),
    ],
)
def test_bounding_box_must_fit_canvas(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BoundingBox.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"cell_range": "A1"}, "requires sheet_name"),
        ({"sheet_name": "Sheet1", "cell_range": "not-a-cell"}, "A1 notation"),
        (
            {
                "bounding_box": {
                    "x": 0,
                    "y": 0,
                    "width": 1,
                    "height": 1,
                    "canvas_width": 1,
                    "canvas_height": 1,
                    "unit": "pixel",
                }
            },
            "requires page_number or asset_id",
        ),
    ],
)
def test_source_location_cross_field_rules(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SourceLocation.model_validate(payload)


def test_source_location_accepts_excel_range() -> None:
    location = SourceLocation(sheet_name="Sheet1", cell_range="$A$1:C9")
    assert location.cell_range == "$A$1:C9"


@pytest.mark.parametrize("name", ["../source.pdf", "folder/source.pdf", "folder\\source.pdf"])
def test_source_and_asset_names_must_be_basenames(name: str) -> None:
    source_payload = make_source_info().model_dump()
    source_payload["name"] = name
    with pytest.raises(ValidationError, match="basename"):
        SourceInfo.model_validate(source_payload)
    with pytest.raises(ValidationError, match="basename"):
        AssetRef(
            asset_id="asset-1",
            filename=name,
            media_type="image/png",
            sha256=ASSET_HASH,
            size_bytes=1,
        )


def test_models_reject_extra_fields_and_naive_timestamps() -> None:
    payload = make_source_info().model_dump()
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        SourceInfo.model_validate(payload)
    with pytest.raises(ValidationError, match="timezone"):
        DocumentMetadata(created_at=datetime(2026, 1, 1))


def test_unordered_list_cannot_have_start() -> None:
    with pytest.raises(ValidationError, match="cannot define start"):
        ListBlock(
            block_id="list-1",
            kind=ListKind.UNORDERED,
            start=1,
            items=(ListItem(blocks=(paragraph("p-1"),)),),
        )


def test_heading_level_is_limited_to_markdown_hierarchy() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 6"):
        HeadingBlock(
            block_id="heading-invalid",
            level=7,
            spans=(TextSpan(text="Too deep"),),
        )


def test_table_rejects_invalid_rows_and_cells() -> None:
    with pytest.raises(ValidationError, match="cover row_count"):
        TableBlock(
            block_id="t-rows",
            row_count=2,
            column_count=1,
            rows=(TableRow(row_index=0),),
        )
    with pytest.raises(ValidationError, match="ordered column indexes"):
        TableBlock(
            block_id="t-order",
            row_count=1,
            column_count=2,
            rows=(
                TableRow(
                    row_index=0,
                    cells=(TableCell(column_index=1), TableCell(column_index=0)),
                ),
            ),
        )
    with pytest.raises(ValidationError, match="exceeds column_count"):
        TableBlock(
            block_id="t-columns",
            row_count=1,
            column_count=1,
            rows=(TableRow(row_index=0, cells=(TableCell(column_index=1),)),),
        )
    with pytest.raises(ValidationError, match="exceeds row_count"):
        TableBlock(
            block_id="t-rowspan",
            row_count=1,
            column_count=1,
            rows=(TableRow(row_index=0, cells=(TableCell(column_index=0, row_span=2),)),),
        )
    with pytest.raises(ValidationError, match="cannot overlap"):
        TableBlock(
            block_id="t-overlap",
            row_count=1,
            column_count=2,
            rows=(
                TableRow(
                    row_index=0,
                    cells=(
                        TableCell(column_index=0, column_span=2),
                        TableCell(column_index=1),
                    ),
                ),
            ),
        )


def test_document_identity_and_reference_invariants() -> None:
    source = make_source_info()
    with pytest.raises(ValidationError, match="derived from source"):
        Document(document_id=f"sha256:{'c' * 64}", source=source)
    with pytest.raises(ValidationError, match="block_id values"):
        Document(
            document_id=f"sha256:{SOURCE_HASH}",
            source=source,
            blocks=(paragraph("duplicate"), paragraph("duplicate")),
        )

    asset = AssetRef(
        asset_id="asset-1",
        filename="image.png",
        media_type="image/png",
        sha256=ASSET_HASH,
        size_bytes=1,
    )
    with pytest.raises(ValidationError, match="asset_id values"):
        Document(
            document_id=f"sha256:{SOURCE_HASH}",
            source=source,
            assets=(asset, asset),
        )
    with pytest.raises(ValidationError, match="every figure"):
        Document(
            document_id=f"sha256:{SOURCE_HASH}",
            source=source,
            blocks=(FigureBlock(block_id="figure", asset_id="missing"),),
        )
