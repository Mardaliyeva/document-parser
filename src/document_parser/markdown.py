"""Deterministic, dependency-free Markdown serialization."""

from __future__ import annotations

import html
import re
from enum import StrEnum
from urllib.parse import quote

from pydantic import field_validator

from document_parser.models import (
    ContainerBlock,
    ContainerRole,
    ContentBlock,
    Document,
    FigureBlock,
    FrozenModel,
    HeadingBlock,
    ListBlock,
    PageBreakBlock,
    ParagraphBlock,
    TableBlock,
    TableCell,
    TextSpan,
)


class FormulaMode(StrEnum):
    """How spreadsheet formulas appear in Markdown tables."""

    DISPLAYED = "displayed"
    FORMULA = "formula"
    BOTH = "both"


class TableMode(StrEnum):
    """Markdown table representation policy."""

    AUTO = "auto"
    GFM = "gfm"
    HTML = "html"


class MarkdownOptions(FrozenModel):
    """Options for canonical RAG-oriented Markdown."""

    include_document_title: bool = True
    include_source_markers: bool = True
    include_headers_footers: bool = False
    include_hidden_sheets: bool = False
    formula_mode: FormulaMode = FormulaMode.DISPLAYED
    table_mode: TableMode = TableMode.AUTO
    asset_prefix: str = "assets/"

    @field_validator("asset_prefix")
    @classmethod
    def validate_asset_prefix(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("asset_prefix cannot contain newlines")
        return value


_MARKDOWN_ESCAPE = re.compile(r"([\\`*_{}\[\]<>#+.!|~-])")


def _escape_text(value: str) -> str:
    return _MARKDOWN_ESCAPE.sub(r"\\\1", value)


def _render_span(span: TextSpan) -> str:
    text = _escape_text(span.text)
    if span.code:
        fence = "``" if "`" in span.text else "`"
        text = f"{fence}{span.text}{fence}"
    if span.bold:
        text = f"**{text}**"
    if span.italic:
        text = f"*{text}*"
    if span.strikethrough:
        text = f"~~{text}~~"
    if span.underline:
        text = f"<u>{text}</u>"
    if span.href:
        text = f"[{text}]({span.href.replace(' ', '%20')})"
    return text


def _render_spans(spans: tuple[TextSpan, ...]) -> str:
    return "".join(_render_span(span) for span in spans)


def _cell_text(cell: TableCell, options: MarkdownOptions) -> str:
    displayed = cell.displayed_text
    if options.formula_mode is FormulaMode.FORMULA and cell.formula:
        return cell.formula
    if options.formula_mode is FormulaMode.BOTH and cell.formula:
        return f"{displayed} ({cell.formula})" if displayed else cell.formula
    return displayed or cell.formula or ""


def _table_grid(table: TableBlock, options: MarkdownOptions) -> list[list[str]]:
    grid = [["" for _ in range(table.column_count)] for _ in range(table.row_count)]
    for row in table.rows:
        for cell in row.cells:
            grid[row.row_index][cell.column_index] = _cell_text(cell, options)
    return grid


def _requires_html(table: TableBlock) -> bool:
    return any(
        cell.row_span > 1
        or cell.column_span > 1
        or bool(cell.blocks)
        or "\n" in cell.displayed_text
        for row in table.rows
        for cell in row.cells
    )


def _render_gfm_table(table: TableBlock, options: MarkdownOptions) -> str:
    grid = _table_grid(table, options)

    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")

    header = grid[0]
    lines = [
        "| " + " | ".join(escape(value) for value in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(escape(value) for value in row) + " |" for row in grid[1:])
    return "\n".join(lines)


def _render_html_table(table: TableBlock, options: MarkdownOptions) -> str:
    lines = ["<table>"]
    for row in table.rows:
        lines.append("  <tr>")
        for cell in row.cells:
            tag = "th" if cell.is_header else "td"
            attributes = ""
            if cell.row_span > 1:
                attributes += f' rowspan="{cell.row_span}"'
            if cell.column_span > 1:
                attributes += f' colspan="{cell.column_span}"'
            value = html.escape(_cell_text(cell, options)).replace("\n", "<br>")
            lines.append(f"    <{tag}{attributes}>{value}</{tag}>")
        lines.append("  </tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _render_table(table: TableBlock, options: MarkdownOptions) -> str:
    use_html = options.table_mode is TableMode.HTML or (
        options.table_mode is TableMode.AUTO and _requires_html(table)
    )
    return _render_html_table(table, options) if use_html else _render_gfm_table(table, options)


def _first_paragraph_text(blocks: tuple[ContentBlock, ...]) -> tuple[str, tuple[ContentBlock, ...]]:
    if blocks and isinstance(blocks[0], ParagraphBlock):
        return _render_spans(blocks[0].spans), blocks[1:]
    return "", blocks


def _render_list(block: ListBlock, options: MarkdownOptions, *, depth: int = 0) -> str:
    lines: list[str] = []
    number = block.start or 1
    for item in block.items:
        marker = f"{number}." if block.kind.value == "ordered" else "-"
        first, remaining = _first_paragraph_text(item.blocks)
        lines.append(f"{'    ' * depth}{marker} {first}".rstrip())
        for nested in remaining:
            if isinstance(nested, ListBlock):
                lines.append(_render_list(nested, options, depth=depth + 1))
            else:
                rendered = _render_block(nested, options, heading_offset=0)
                lines.extend(f"{'    ' * (depth + 1)}{line}" for line in rendered.splitlines())
        number += 1
    return "\n".join(lines)


def _render_container(
    block: ContainerBlock,
    options: MarkdownOptions,
    *,
    heading_offset: int,
) -> str:
    visibility = str(block.attributes.get("visibility", "visible"))
    if (
        block.role is ContainerRole.SHEET
        and visibility != "visible"
        and not options.include_hidden_sheets
    ):
        return ""

    parts: list[str] = []
    child_offset = heading_offset
    if block.role is ContainerRole.PAGE and options.include_source_markers:
        page_number = block.source.page_number if block.source else None
        if page_number is not None:
            parts.append(f"<!-- page: {page_number} -->")
    elif block.role is ContainerRole.SHEET:
        title = _render_spans(block.title) or str(block.attributes.get("sheet_name", "Sheet"))
        level = min(6, 1 + heading_offset)
        parts.append(f"{'#' * level} Sheet: {title}")
        child_offset += 1
    elif block.title:
        level = min(6, 1 + heading_offset)
        parts.append(f"{'#' * level} {_render_spans(block.title)}")
        child_offset += 1

    parts.extend(
        rendered
        for child in block.blocks
        if (rendered := _render_block(child, options, heading_offset=child_offset))
    )
    return "\n\n".join(parts)


def _render_block(block: ContentBlock, options: MarkdownOptions, *, heading_offset: int) -> str:
    if block.attributes.get("active_for_rag") is False:
        return ""
    story = str(block.attributes.get("story", ""))
    if story in {"header", "footer"} and not options.include_headers_footers:
        return ""
    if isinstance(block, ContainerBlock):
        return _render_container(block, options, heading_offset=heading_offset)
    if isinstance(block, HeadingBlock):
        level = min(6, block.level + heading_offset)
        return f"{'#' * level} {_render_spans(block.spans)}"
    if isinstance(block, ParagraphBlock):
        return _render_spans(block.spans)
    if isinstance(block, ListBlock):
        return _render_list(block, options)
    if isinstance(block, TableBlock):
        return _render_table(block, options)
    if isinstance(block, FigureBlock):
        alt = block.alt_text or _render_spans(block.caption) or "image"
        filename = block.asset_id
        return f"![{_escape_text(alt)}]({quote(options.asset_prefix + filename, safe='/:')})"
    if isinstance(block, PageBreakBlock):
        return "---"
    raise TypeError(f"unsupported block type: {type(block).__name__}")


def to_markdown(document: Document, *, options: MarkdownOptions | None = None) -> str:
    """Serialize a validated Document into canonical Markdown."""

    resolved = options or MarkdownOptions()
    asset_names = {asset.asset_id: asset.filename for asset in document.assets}
    parts: list[str] = []
    heading_offset = 0
    if resolved.include_document_title and document.metadata.title:
        parts.append(f"# {_escape_text(document.metadata.title)}")
        heading_offset = 1
    for block in document.blocks:
        rendered = _render_block(block, resolved, heading_offset=heading_offset)
        for asset_id, filename in asset_names.items():
            rendered = rendered.replace(
                f"{quote(resolved.asset_prefix + asset_id, safe='/:')})",
                f"{quote(resolved.asset_prefix + filename, safe='/:')})",
            )
        if rendered:
            parts.append(rendered)
    output = "\n\n".join(parts).strip()
    return f"{output}\n" if output else ""
