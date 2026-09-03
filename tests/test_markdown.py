"""Tests for canonical Markdown and conversion result validation."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from document_parser import (
    AdapterOutput,
    AssetPayload,
    AssetRef,
    ContainerBlock,
    ContainerRole,
    Document,
    DocumentFormat,
    DocumentMetadata,
    FigureBlock,
    FormulaMode,
    HeadingBlock,
    ListBlock,
    ListItem,
    ListKind,
    MarkdownOptions,
    ParagraphBlock,
    SourceInfo,
    TableBlock,
    TableCell,
    TableMode,
    TableRow,
    TextSpan,
    to_markdown,
)


def source() -> SourceInfo:
    digest = "a" * 64
    return SourceInfo(
        name="sample.xlsx",
        size_bytes=10,
        sha256=digest,
        format=DocumentFormat.XLSX,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def test_markdown_renders_structure_formatting_tables_and_assets() -> None:
    data = b"image"
    digest = hashlib.sha256(data).hexdigest()
    ref = AssetRef(
        asset_id=f"asset:sha256:{digest}",
        filename=f"sha256-{digest}.png",
        media_type="image/png",
        sha256=digest,
        size_bytes=len(data),
    )
    simple = TableBlock(
        block_id="table-simple",
        row_count=2,
        column_count=2,
        rows=(
            TableRow(
                row_index=0,
                cells=(
                    TableCell(column_index=0, is_header=True, displayed_text="A|B"),
                    TableCell(column_index=1, is_header=True, displayed_text="Formula"),
                ),
            ),
            TableRow(
                row_index=1,
                cells=(
                    TableCell(column_index=0, displayed_text="One"),
                    TableCell(column_index=1, displayed_text="3", formula="=1+2"),
                ),
            ),
        ),
    )
    merged = TableBlock(
        block_id="table-merged",
        row_count=1,
        column_count=2,
        rows=(
            TableRow(
                row_index=0,
                cells=(TableCell(column_index=0, column_span=2, displayed_text="Merged"),),
            ),
        ),
    )
    nested = ListBlock(
        block_id="list",
        kind=ListKind.ORDERED,
        start=2,
        items=(
            ListItem(
                blocks=(
                    ParagraphBlock(block_id="list-p", spans=(TextSpan(text="Item"),)),
                    ListBlock(
                        block_id="nested",
                        kind=ListKind.UNORDERED,
                        items=(
                            ListItem(
                                blocks=(
                                    ParagraphBlock(
                                        block_id="nested-p", spans=(TextSpan(text="Nested"),)
                                    ),
                                )
                            ),
                        ),
                    ),
                )
            ),
        ),
    )
    hidden = ContainerBlock(
        block_id="hidden",
        role=ContainerRole.SHEET,
        title=(TextSpan(text="Hidden"),),
        attributes={"visibility": "hidden"},
        blocks=(ParagraphBlock(block_id="secret", spans=(TextSpan(text="Secret"),)),),
    )
    document = Document(
        document_id=f"sha256:{source().sha256}",
        source=source(),
        metadata=DocumentMetadata(title="A # title"),
        blocks=(
            HeadingBlock(block_id="heading", level=1, spans=(TextSpan(text="Heading"),)),
            ParagraphBlock(
                block_id="formatted",
                spans=(
                    TextSpan(
                        text="value",
                        bold=True,
                        italic=True,
                        underline=True,
                        strikethrough=True,
                        href="https://example.test/a b",
                    ),
                ),
            ),
            nested,
            simple,
            merged,
            FigureBlock(block_id="figure", asset_id=ref.asset_id, alt_text="Diagram"),
            hidden,
        ),
        assets=(ref,),
    )

    markdown = to_markdown(
        document,
        options=MarkdownOptions(formula_mode=FormulaMode.BOTH),
    )

    assert markdown.startswith("# A \\# title\n")
    assert "## Heading" in markdown
    assert "[<u>~~***value***~~</u>](https://example.test/a%20b)" in markdown
    assert "2. Item\n    - Nested" in markdown
    assert "A\\|B" in markdown
    assert "3 (=1+2)" in markdown
    assert '<td colspan="2">Merged</td>' in markdown
    assert f"assets/sha256-{digest}.png" in markdown
    assert "Secret" not in markdown
    assert markdown.endswith("\n") and not markdown.endswith("\n\n")


def test_markdown_options_control_tables_formulas_and_hidden_sheets() -> None:
    table = TableBlock(
        block_id="table",
        row_count=1,
        column_count=1,
        rows=(
            TableRow(
                row_index=0,
                cells=(TableCell(column_index=0, displayed_text="cached", formula="=A1"),),
            ),
        ),
    )
    sheet = ContainerBlock(
        block_id="sheet",
        role=ContainerRole.SHEET,
        title=(TextSpan(text="Hidden"),),
        attributes={"visibility": "hidden"},
        blocks=(table,),
    )
    document = Document(document_id=f"sha256:{source().sha256}", source=source(), blocks=(sheet,))
    markdown = to_markdown(
        document,
        options=MarkdownOptions(
            include_hidden_sheets=True,
            formula_mode=FormulaMode.FORMULA,
            table_mode=TableMode.HTML,
        ),
    )
    assert "# Sheet: Hidden" in markdown
    assert "<td>=A1</td>" in markdown


def test_asset_payload_and_adapter_output_validate_manifests() -> None:
    data = b"payload"
    digest = hashlib.sha256(data).hexdigest()
    ref = AssetRef(
        asset_id=f"asset:sha256:{digest}",
        filename="asset.bin",
        media_type="application/octet-stream",
        sha256=digest,
        size_bytes=len(data),
    )
    payload = AssetPayload(ref=ref, data=data)
    document = Document(document_id=f"sha256:{source().sha256}", source=source(), assets=(ref,))
    assert AdapterOutput(document=document, assets=(payload,)).model_dump_json()

    with pytest.raises(ValidationError, match="size"):
        AssetPayload(ref=ref, data=b"x")
    wrong_hash = ref.model_copy(update={"sha256": "b" * 64})
    with pytest.raises(ValidationError, match="hash"):
        AssetPayload(ref=wrong_hash, data=data)
    with pytest.raises(ValidationError, match="exactly match"):
        AdapterOutput(document=document)
