"""Focused branch tests for Phase 3 helpers and serializer options."""

from __future__ import annotations

import hashlib
from typing import ClassVar, cast

import pytest
from pydantic import ValidationError

from document_parser import (
    AdapterOutput,
    AssetPayload,
    AssetRef,
    ContainerBlock,
    ContainerRole,
    ContentBlock,
    Document,
    DocumentFormat,
    FigureBlock,
    HeadingBlock,
    ListBlock,
    ListItem,
    ListKind,
    MarkdownOptions,
    PageBreakBlock,
    ParagraphBlock,
    ParseOptions,
    SourceInfo,
    SourceLocation,
    TableBlock,
    TableCell,
    TableMode,
    TableRow,
    TextSpan,
    UnsafeDocumentError,
    to_markdown,
)
from document_parser._adapter_utils import (
    AssetCollector,
    joined_text,
    normalize_media_type,
    normalize_text,
)
from document_parser.markdown import _render_block


def make_source() -> SourceInfo:
    return SourceInfo(
        name="sample.pdf",
        size_bytes=1,
        sha256="a" * 64,
        format=DocumentFormat.PDF,
        media_type="application/pdf",
    )


def make_document(*blocks: ContentBlock, assets: tuple[AssetRef, ...] = ()) -> Document:
    return Document(
        document_id=f"sha256:{make_source().sha256}",
        source=make_source(),
        blocks=blocks,
        assets=assets,
    )


def test_text_media_and_join_helpers_cover_normalization_fallbacks() -> None:
    assert normalize_text("e\u0301\r\nA\x00B\t") == "é\nAB\t"
    assert normalize_media_type("IMAGE/PNG; charset=x") == "image/png"
    assert normalize_media_type(None, "photo.jpg") == "image/jpeg"
    assert normalize_media_type(None, "unknown") == "application/octet-stream"
    assert joined_text([" one ", "", " two"]) == "one two"


def test_asset_collector_deduplicates_names_and_enforces_all_limits() -> None:
    collector = AssetCollector(
        ParseOptions(max_assets=2, max_asset_bytes=3, max_total_asset_bytes=4), "source.docx"
    )
    first = collector.add(b"aaa", filename="unsafe.longextension", media_type="image/png")
    assert first.filename.endswith(".png")
    assert collector.add(b"aaa", filename="other.jpg") is first
    with pytest.raises(UnsafeDocumentError, match="max_total_asset_bytes"):
        collector.add(b"bb", filename="b.bin")

    count_limited = AssetCollector(
        ParseOptions(max_assets=1, max_asset_bytes=2, max_total_asset_bytes=4), "source.docx"
    )
    count_limited.add(b"a", filename="a.bin")
    with pytest.raises(UnsafeDocumentError, match="too many assets"):
        count_limited.add(b"b", filename="b.bin")

    size_limited = AssetCollector(
        ParseOptions(max_asset_bytes=1, max_total_asset_bytes=2), "source.docx"
    )
    with pytest.raises(UnsafeDocumentError, match="max_asset_bytes"):
        size_limited.add(b"ab")


def test_parse_and_markdown_options_reject_invalid_limits_and_prefix() -> None:
    with pytest.raises(ValidationError, match="max_asset_bytes"):
        ParseOptions(max_asset_bytes=2, max_total_asset_bytes=1)
    with pytest.raises(ValidationError, match="newlines"):
        MarkdownOptions(asset_prefix="assets/\n")
    assert MarkdownOptions(asset_prefix="media/").asset_prefix == "media/"


def test_adapter_output_rejects_duplicate_payload_ids() -> None:
    data = b"a"
    digest = hashlib.sha256(data).hexdigest()
    ref = AssetRef(
        asset_id=f"asset:sha256:{digest}",
        filename="a.bin",
        media_type="application/octet-stream",
        sha256=digest,
        size_bytes=1,
    )
    payload = AssetPayload(ref=ref, data=data)
    document = make_document(assets=(ref,))
    with pytest.raises(ValidationError, match="unique"):
        AdapterOutput(document=document, assets=(payload, payload))


def test_markdown_covers_code_fences_container_modes_and_page_breaks() -> None:
    header = ContainerBlock(
        block_id="header",
        role=ContainerRole.SECTION,
        attributes={"story": "header"},
        blocks=(ParagraphBlock(block_id="header-p", spans=(TextSpan(text="Header"),)),),
    )
    page_without_location = ContainerBlock(
        block_id="page-none",
        role=ContainerRole.PAGE,
        blocks=(ParagraphBlock(block_id="code", spans=(TextSpan(text="a`b", code=True),)),),
    )
    page_with_location = ContainerBlock(
        block_id="page",
        role=ContainerRole.PAGE,
        source=SourceLocation(page_number=2),
        blocks=(
            ParagraphBlock(
                block_id="hidden-line",
                spans=(TextSpan(text="Repeated"),),
                attributes={"story": "footer"},
            ),
            PageBreakBlock(block_id="break"),
        ),
    )
    titled_section = ContainerBlock(
        block_id="section",
        role=ContainerRole.SECTION,
        title=(TextSpan(text="Notes"),),
    )
    untitled_sheet = ContainerBlock(
        block_id="sheet",
        role=ContainerRole.SHEET,
        attributes={"sheet_name": "Fallback", "visibility": "visible"},
    )
    document = make_document(
        header, page_without_location, page_with_location, titled_section, untitled_sheet
    )

    markdown = to_markdown(document)
    assert "Header" not in markdown and "Repeated" not in markdown
    assert "``a`b``" in markdown
    assert "<!-- page: 2 -->" in markdown
    assert "---" in markdown
    assert "# Notes" in markdown
    assert "# Sheet: Fallback" in markdown

    included = to_markdown(
        document,
        options=MarkdownOptions(include_headers_footers=True, include_source_markers=False),
    )
    assert "Header" in included and "Repeated" in included
    assert "<!-- page:" not in included


def test_markdown_covers_nonparagraph_list_items_figures_and_empty_output() -> None:
    data = b"image"
    digest = hashlib.sha256(data).hexdigest()
    ref = AssetRef(
        asset_id=f"asset:sha256:{digest}",
        filename="image.png",
        media_type="image/png",
        sha256=digest,
        size_bytes=len(data),
    )
    block = ListBlock(
        block_id="list",
        kind=ListKind.UNORDERED,
        items=(
            ListItem(
                blocks=(
                    HeadingBlock(block_id="item-heading", level=2, spans=(TextSpan(text="H"),)),
                    TableBlock(
                        block_id="item-table",
                        row_count=1,
                        column_count=1,
                        rows=(
                            TableRow(
                                row_index=0,
                                cells=(TableCell(column_index=0, displayed_text="Cell"),),
                            ),
                        ),
                    ),
                )
            ),
        ),
    )
    figures = (
        FigureBlock(block_id="caption", asset_id=ref.asset_id, caption=(TextSpan(text="Cap"),)),
        FigureBlock(block_id="fallback", asset_id=ref.asset_id),
    )
    markdown = to_markdown(make_document(block, *figures, assets=(ref,)))
    assert "-\n    ## H" in markdown
    assert "    | Cell |" in markdown
    assert "![Cap]" in markdown and "![image]" in markdown
    assert to_markdown(make_document()) == ""


def test_markdown_forced_gfm_and_unknown_block_guard() -> None:
    table = TableBlock(
        block_id="merged",
        row_count=1,
        column_count=2,
        rows=(
            TableRow(
                row_index=0,
                cells=(TableCell(column_index=0, column_span=2, displayed_text="Merged"),),
            ),
        ),
    )
    assert "| Merged |" in to_markdown(
        make_document(table), options=MarkdownOptions(table_mode=TableMode.GFM)
    )

    class Unknown:
        attributes: ClassVar[dict[str, object]] = {}

    with pytest.raises(TypeError, match="unsupported block"):
        _render_block(cast(ContentBlock, Unknown()), MarkdownOptions(), heading_offset=0)
