"""Deterministic, dependency-free Markdown serialization."""

from __future__ import annotations

import html
import re
from enum import StrEnum
from urllib.parse import quote, urlsplit

from pydantic import field_validator, model_validator

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
    RECORDS = "records"


class MarkdownProfile(StrEnum):
    """Clean semantic output or layout-oriented legacy presentation."""

    CLEAN = "clean"
    FIDELITY = "fidelity"


class MarkdownOptions(FrozenModel):
    """Options for canonical RAG-oriented Markdown."""

    profile: MarkdownProfile = MarkdownProfile.CLEAN
    include_document_title: bool = True
    include_source_markers: bool | None = None
    include_headers_footers: bool = False
    include_hidden_sheets: bool = False
    formula_mode: FormulaMode = FormulaMode.DISPLAYED
    table_mode: TableMode = TableMode.AUTO
    asset_prefix: str = "assets/"

    @model_validator(mode="after")
    def validate_profile(self) -> MarkdownOptions:
        if self.profile is MarkdownProfile.CLEAN and (
            self.table_mode is TableMode.HTML or self.include_source_markers is True
        ):
            raise ValueError("HTML tables and source comments require the fidelity profile")
        return self

    @field_validator("asset_prefix")
    @classmethod
    def validate_asset_prefix(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("asset_prefix cannot contain newlines")
        return value


_MARKDOWN_ESCAPE = re.compile(r"([\\`*_\[\]<>|~])")


def _escape_text(value: str) -> str:
    escaped = _MARKDOWN_ESCAPE.sub(r"\\\1", value)

    def escape_marker(match: re.Match[str]) -> str:
        prefix, marker = match.groups()
        return prefix + (marker[:-1] + "\\" + marker[-1] if marker[0].isdigit() else "\\" + marker)

    return re.sub(r"(?m)^([ \t]*)(#{1,6}(?=\s)|[-+](?=\s)|\d+[.)](?=\s))", escape_marker, escaped)


def _render_span(span: TextSpan, options: MarkdownOptions | None = None) -> str:
    resolved = options or MarkdownOptions()
    raw = span.text.strip()
    if not raw:
        return span.text
    leading = span.text[: len(span.text) - len(span.text.lstrip())]
    trailing = span.text[len(span.text.rstrip()) :]
    text = _escape_text(raw)
    if span.code:
        fence = "`" * (max((len(m.group()) for m in re.finditer(r"`+", raw)), default=0) + 1)
        padded = f" {raw} " if raw.startswith("`") or raw.endswith("`") else raw
        text = f"{fence}{padded}{fence}"
    if span.bold:
        text = f"**{text}**"
    if span.italic:
        text = f"*{text}*"
    if span.strikethrough:
        text = f"~~{text}~~"
    if span.underline and resolved.profile is MarkdownProfile.FIDELITY:
        text = f"<u>{text}</u>"
    if span.href:
        try:
            scheme = urlsplit(span.href).scheme.lower()
        except ValueError:
            scheme = "invalid"
        if scheme in {"", "http", "https", "mailto", "tel"}:
            target = quote(span.href, safe="/:#?&=%@+;,$~!'")
            text = f"[{text}]({target})"
    return leading + text + trailing


def _render_spans(spans: tuple[TextSpan, ...], options: MarkdownOptions | None = None) -> str:
    return "".join(_render_span(span, options) for span in spans)


def _cell_text(cell: TableCell, options: MarkdownOptions) -> str:
    displayed = cell.displayed_text or "\n".join(
        "".join(span.text for span in block.spans)
        for block in cell.blocks
        if isinstance(block, (ParagraphBlock, HeadingBlock))
    )
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
        or any(not isinstance(block, ParagraphBlock) for block in cell.blocks)
        or len(cell.blocks) > 1
        or "\n" in cell.displayed_text
        for row in table.rows
        for cell in row.cells
    )


def _render_gfm_table(table: TableBlock, options: MarkdownOptions) -> str:
    grid = _table_grid(table, options)

    def escape(value: str) -> str:
        return _escape_text(value).replace("\n", "<br>")

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


def _render_records(table: TableBlock, options: MarkdownOptions) -> str:
    """Keep merged contexts and nested content without emitting HTML."""
    grid: list[list[TableCell | None]] = [
        [None] * table.column_count for _ in range(table.row_count)
    ]
    for source_row in table.rows:
        for source_cell in source_row.cells:
            for ri in range(source_row.row_index, source_row.row_index + source_cell.row_span):
                for ci in range(
                    source_cell.column_index, source_cell.column_index + source_cell.column_span
                ):
                    grid[ri][ci] = source_cell
    header_count = 0
    for row in grid:
        if any(cell is not None for cell in row) and all(
            cell is None or cell.is_header for cell in row
        ):
            header_count += 1
        else:
            break
    # Header-only forms still carry meaningful field names.
    if header_count == len(grid):
        header_count = 0
    labels: list[str] = []
    for column in range(table.column_count):
        parts: list[str] = []
        for row in grid[:header_count]:
            cell = row[column]
            text = _cell_text(cell, options).strip() if cell else ""
            if text and text not in parts:
                parts.append(text.replace("\n", " / "))
        labels.append(" / ".join(parts) or f"Sütun {column + 1}")
    records: list[str] = []
    for index, row in enumerate(grid[header_count:], start=1):
        lines = [f"- **Sətir {index}**"]
        seen: set[int] = set()
        for column, cell in enumerate(row):
            if cell is not None and id(cell) in seen:
                continue
            if cell is not None:
                seen.add(id(cell))
            covered = labels[column : column + (cell.column_span if cell else 1)]
            label = " / ".join(dict.fromkeys(covered))
            value = _cell_text(cell, options) if cell else ""
            if cell and cell.blocks and not cell.formula:
                nested_value = "\n\n".join(
                    rendered
                    for child in cell.blocks
                    if (rendered := _render_block(child, options, heading_offset=0))
                )
                value = nested_value or _escape_text(value)
            else:
                value = _escape_text(value)
            lines.append(f"    - **{_escape_text(label)}:**")
            if value:
                lines.append("")
                lines.extend(f"        {line}" if line else "" for line in value.splitlines())
        records.append("\n".join(lines))
    return "\n\n".join(records)


def _render_table(table: TableBlock, options: MarkdownOptions) -> str:
    if options.table_mode is TableMode.RECORDS or (
        options.profile is MarkdownProfile.CLEAN
        and (
            _requires_html(table)
            or any("\n" in _cell_text(cell, options) for row in table.rows for cell in row.cells)
            or not any(cell.is_header for row in table.rows[:1] for cell in row.cells)
        )
    ):
        return _render_records(table, options)
    use_html = options.table_mode is TableMode.HTML or (
        options.table_mode is TableMode.AUTO and _requires_html(table)
    )
    return _render_html_table(table, options) if use_html else _render_gfm_table(table, options)


def _first_paragraph_text(
    blocks: tuple[ContentBlock, ...], options: MarkdownOptions | None = None
) -> tuple[str, tuple[ContentBlock, ...]]:
    if blocks and isinstance(blocks[0], ParagraphBlock):
        return _render_spans(blocks[0].spans, options), blocks[1:]
    return "", blocks


def _render_list(block: ListBlock, options: MarkdownOptions, *, depth: int = 0) -> str:
    lines: list[str] = []
    number = block.start or 1
    for item in block.items:
        marker = f"{number}." if block.kind.value == "ordered" else "-"
        first, remaining = _first_paragraph_text(item.blocks, options)
        original_label = item.blocks[0].attributes.get("numbering_label")
        if isinstance(original_label, str) and original_label and original_label != marker:
            if re.fullmatch(r"\d+[.)]", original_label):
                marker = original_label
            else:
                marker = "-"
                first = f"{_escape_text(original_label)} {first}"
        first_lines = first.splitlines() or [""]
        lines.append(f"{'    ' * depth}{marker} {first_lines[0]}".rstrip())
        lines.extend(f"{'    ' * (depth + 1)}{line}" for line in first_lines[1:])
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
    if (
        block.role is ContainerRole.PAGE
        and options.profile is MarkdownProfile.FIDELITY
        and options.include_source_markers is not False
    ):
        page_number = block.source.page_number if block.source else None
        if page_number is not None:
            parts.append(f"<!-- page: {page_number} -->")
    elif block.role is ContainerRole.SHEET:
        title = _render_spans(block.title, options) or str(
            block.attributes.get("sheet_name", "Sheet")
        )
        level = min(6, 1 + heading_offset)
        parts.append(f"{'#' * level} Sheet: {title}")
        child_offset += 1
    elif block.title:
        level = min(6, 1 + heading_offset)
        parts.append(f"{'#' * level} {_render_spans(block.title, options)}")
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
        return f"{'#' * level} {_render_spans(block.spans, options)}"
    if isinstance(block, ParagraphBlock):
        return _render_spans(block.spans, options)
    if isinstance(block, ListBlock):
        return _render_list(block, options)
    if isinstance(block, TableBlock):
        return _render_table(block, options)
    if isinstance(block, FigureBlock):
        if options.profile is MarkdownProfile.CLEAN:
            return _render_spans(block.caption, options)
        alt = block.alt_text or _render_spans(block.caption) or "image"
        filename = block.asset_id
        return f"![{_escape_text(alt)}]({quote(options.asset_prefix + filename, safe='/:')})"
    if isinstance(block, PageBreakBlock):
        return "---" if options.profile is MarkdownProfile.FIDELITY else ""
    raise TypeError(f"unsupported block type: {type(block).__name__}")


def to_markdown(document: Document, *, options: MarkdownOptions | None = None) -> str:
    """Serialize a validated Document into canonical Markdown."""

    resolved = options or MarkdownOptions()
    asset_names = {asset.asset_id: asset.filename for asset in document.assets}
    parts: list[str] = []
    heading_offset = 0

    def has_title(blocks: tuple[ContentBlock, ...], title: str) -> bool:
        for block in blocks:
            if block.attributes.get("active_for_rag") is False or block.attributes.get("story") in {
                "header",
                "footer",
            }:
                continue
            if (
                isinstance(block, (HeadingBlock, ParagraphBlock))
                and " ".join("".join(span.text for span in block.spans).split()).casefold() == title
            ):
                return True
            if isinstance(block, ContainerBlock) and has_title(block.blocks, title):
                return True
        return False

    if (
        resolved.include_document_title
        and document.metadata.title
        and not has_title(document.blocks, " ".join(document.metadata.title.split()).casefold())
    ):
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
