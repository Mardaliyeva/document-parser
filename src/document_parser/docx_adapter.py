"""Native DOCX adapter with lazy third-party imports."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from document_parser._adapter_utils import AssetCollector, normalize_text
from document_parser.exceptions import InvalidDocumentError
from document_parser.models import (
    ContainerBlock,
    ContainerRole,
    ContentBlock,
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
    SourceLocation,
    TableBlock,
    TableCell,
    TableRow,
    TextSpan,
)
from document_parser.results import AdapterOutput
from document_parser.sources import AdapterInput, ParseOptions

if TYPE_CHECKING:
    from docx.document import Document as WordDocument
    from docx.section import Section
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph
    from docx.text.run import Run


@dataclass(frozen=True, slots=True)
class _ListSpec:
    kind: ListKind
    level: int
    start: int


@dataclass(frozen=True, slots=True)
class _ListEntry:
    spec: _ListSpec
    blocks: tuple[ContentBlock, ...]


class _DocxContext:
    __slots__ = (
        "assets",
        "diagnostics",
        "numbering",
        "options",
        "partial",
        "sequence",
    )

    def __init__(
        self,
        options: ParseOptions,
        source_name: str,
        numbering: dict[tuple[int, int], tuple[ListKind, int]],
    ) -> None:
        self.options = options
        self.assets = AssetCollector(options, source_name)
        self.diagnostics: list[Diagnostic] = []
        self.numbering = numbering
        self.partial = False
        self.sequence = 0

    def next_id(self, kind: str) -> str:
        self.sequence += 1
        return f"docx:{kind}:{self.sequence:06d}"

    def location(self) -> SourceLocation:
        return SourceLocation(block_index=self.sequence)

    def warn(self, code: str, message: str, *, partial: bool = False) -> None:
        self.diagnostics.append(
            Diagnostic(code=code, message=message, severity=DiagnosticSeverity.WARNING)
        )
        self.partial = self.partial or partial


class DocxAdapter:
    """Convert WordprocessingML content into Document IR."""

    format = DocumentFormat.DOCX

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        from docx import Document as load_document

        with source.open_binary() as stream:
            data = stream.read()
        try:
            word = load_document(io.BytesIO(data))
        except Exception as exc:
            raise InvalidDocumentError(
                "DOCX package could not be opened", source_name=source.info.name
            ) from exc

        context = _DocxContext(options, source.info.name, _numbering_map(word))
        section_items = _partition_sections(word)
        sections: list[ContentBlock] = []
        seen_stories: set[str] = set()
        for section_index, items in enumerate(section_items):
            body_blocks = list(_story_blocks(items, context))
            if section_index < len(word.sections):
                body_blocks.extend(
                    _section_stories(
                        word.sections[section_index],
                        section_index,
                        context,
                        seen_stories,
                    )
                )
            block_id = context.next_id("section")
            sections.append(
                ContainerBlock(
                    block_id=block_id,
                    role=ContainerRole.SECTION,
                    source=context.location(),
                    attributes={"section_index": section_index},
                    blocks=tuple(body_blocks),
                )
            )

        sections.extend(_note_stories(data, context))
        if _contains_deleted_revision(data):
            context.warn(
                "docx.deleted_revision_omitted",
                "Deleted revision text was omitted; inserted revision text was retained.",
            )

        metadata = _metadata(word)
        status = DocumentStatus.PARTIAL if context.partial else DocumentStatus.COMPLETE
        document = Document(
            document_id=f"sha256:{source.info.sha256}",
            source=source.info,
            metadata=metadata,
            blocks=tuple(sections),
            assets=context.assets.refs,
            status=status,
            diagnostics=tuple((*source.info.diagnostics, *context.diagnostics)),
        )
        return AdapterOutput(document=document, assets=context.assets.payloads)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _metadata(word: WordDocument) -> DocumentMetadata:
    properties = word.core_properties
    authors = (normalize_text(properties.author),) if properties.author else ()
    keywords = tuple(
        part.strip()
        for part in normalize_text(properties.keywords or "").split(",")
        if part.strip()
    )
    return DocumentMetadata(
        title=normalize_text(properties.title) if properties.title else None,
        authors=authors,
        subject=normalize_text(properties.subject) if properties.subject else None,
        keywords=keywords,
        language=normalize_text(properties.language) if properties.language else None,
        created_at=_aware(properties.created),
        modified_at=_aware(properties.modified),
        custom={"category": normalize_text(properties.category)} if properties.category else {},
    )


def _partition_sections(word: WordDocument) -> tuple[tuple[object, ...], ...]:
    from docx.text.paragraph import Paragraph

    grouped: list[tuple[object, ...]] = []
    current: list[object] = []
    for item in word.iter_inner_content():
        current.append(item)
        if (
            isinstance(item, Paragraph)
            and item._p.pPr is not None
            and item._p.pPr.sectPr is not None
        ):
            grouped.append(tuple(current))
            current = []
    if current or not grouped:
        grouped.append(tuple(current))
    return tuple(grouped)


def _numbering_map(word: WordDocument) -> dict[tuple[int, int], tuple[ListKind, int]]:
    from docx.oxml.ns import qn

    try:
        root = word.part.numbering_part.element
    except (AttributeError, KeyError):
        return {}
    abstracts: dict[int, dict[int, tuple[ListKind, int]]] = {}
    for abstract in root.findall(qn("w:abstractNum")):
        abstract_id = int(abstract.get(qn("w:abstractNumId")))
        levels: dict[int, tuple[ListKind, int]] = {}
        for level in abstract.findall(qn("w:lvl")):
            level_index = int(level.get(qn("w:ilvl")))
            format_element = level.find(qn("w:numFmt"))
            start_element = level.find(qn("w:start"))
            format_name = (
                format_element.get(qn("w:val")) if format_element is not None else "bullet"
            )
            start = int(start_element.get(qn("w:val"))) if start_element is not None else 1
            kind = ListKind.UNORDERED if format_name in {"bullet", "none"} else ListKind.ORDERED
            levels[level_index] = (kind, start)
        abstracts[abstract_id] = levels

    result: dict[tuple[int, int], tuple[ListKind, int]] = {}
    for number in root.findall(qn("w:num")):
        number_id = int(number.get(qn("w:numId")))
        abstract_element = number.find(qn("w:abstractNumId"))
        if abstract_element is None:
            continue
        abstract_id = int(abstract_element.get(qn("w:val")))
        for level, spec in abstracts.get(abstract_id, {}).items():
            result[(number_id, level)] = spec
    return result


def _list_spec(paragraph: Paragraph, context: _DocxContext) -> _ListSpec | None:
    properties = paragraph._p.pPr
    number_properties = cast(Any, properties.numPr if properties is not None else None)
    if number_properties is not None and number_properties.numId is not None:
        number_id = int(number_properties.numId.val)
        level = int(number_properties.ilvl.val) if number_properties.ilvl is not None else 0
        kind, start = context.numbering.get((number_id, level), (ListKind.UNORDERED, 1))
        return _ListSpec(kind=kind, level=level, start=start)

    style_name = (paragraph.style.name or "").lower() if paragraph.style else ""
    if style_name.startswith("list"):
        kind = ListKind.ORDERED if "number" in style_name else ListKind.UNORDERED
        match = re.search(r"(\d+)$", style_name)
        level = max(0, int(match.group(1)) - 1) if match else 0
        return _ListSpec(kind=kind, level=level, start=1)
    return None


def _heading_level(paragraph: Paragraph) -> int | None:
    style_name = paragraph.style.name if paragraph.style else ""
    match = re.search(r"heading\s*([1-9])", style_name, re.IGNORECASE)
    if match:
        return min(6, int(match.group(1)))
    properties = paragraph._p.pPr
    outline = properties.outlineLvl if properties is not None else None
    if outline is not None:
        return min(6, int(outline.val) + 1)
    return None


def _text_block(
    spans: list[TextSpan], paragraph: Paragraph, context: _DocxContext
) -> ContentBlock | None:
    if not spans:
        return None
    level = _heading_level(paragraph)
    kind = "heading" if level is not None else "paragraph"
    block_id = context.next_id(kind)
    if level is not None:
        return HeadingBlock(
            block_id=block_id,
            level=level,
            spans=tuple(spans),
            source=context.location(),
            attributes={"style": paragraph.style.name if paragraph.style else ""},
        )
    return ParagraphBlock(
        block_id=block_id,
        spans=tuple(spans),
        source=context.location(),
        attributes={"style": paragraph.style.name if paragraph.style else ""},
    )


def _run_span(run: Run, href: str | None, context: _DocxContext) -> TextSpan | None:
    if run.font.hidden and not context.options.docx.include_hidden_text:
        return None
    text = normalize_text(run.text)
    if not text:
        return None
    style_name = (run.style.name or "").lower() if run.style else ""
    return TextSpan(
        text=text,
        bold=bool(run.bold),
        italic=bool(run.italic),
        underline=bool(run.underline),
        strikethrough=bool(run.font.strike),
        code="code" in style_name or "source" in style_name,
        href=href,
    )


def _run_figures(run: Run, context: _DocxContext) -> tuple[FigureBlock, ...]:
    from docx.oxml.ns import qn

    figures: list[FigureBlock] = []
    embedded = cast(list[str], run._r.xpath(".//a:blip/@r:embed"))
    linked = cast(list[str], run._r.xpath(".//a:blip/@r:link"))
    if linked:
        context.warn(
            "docx.external_image_omitted",
            "An externally linked image was not fetched.",
            partial=True,
        )
    descriptions = cast(list[str], run._r.xpath(".//wp:docPr/@descr"))
    titles = cast(list[str], run._r.xpath(".//wp:docPr/@title"))
    alt_text = next((value for value in (*descriptions, *titles) if value), None)
    for relationship_id in embedded:
        part = run.part.related_parts.get(relationship_id)
        if part is None or not hasattr(part, "blob"):
            context.warn(
                "docx.image_missing", "An embedded image relationship was missing.", partial=True
            )
            continue
        data = bytes(part.blob)
        ref = context.assets.add(
            data,
            filename=str(part.partname).rsplit("/", maxsplit=1)[-1],
            media_type=str(part.content_type),
        )
        block_id = context.next_id("figure")
        figures.append(
            FigureBlock(
                block_id=block_id,
                asset_id=ref.asset_id,
                alt_text=normalize_text(alt_text) if alt_text else None,
                source=SourceLocation(block_index=context.sequence, asset_id=ref.asset_id),
            )
        )
    for tag, code, message in (
        (qn("c:chart"), "docx.chart_omitted", "A chart could not be rendered."),
        (qn("dgm:relIds"), "docx.smartart_omitted", "SmartArt could not be rendered."),
        (qn("m:oMath"), "docx.equation_simplified", "An equation was reduced to visible text."),
    ):
        if any(element.tag == tag for element in run._r.iter()):
            context.warn(code, message, partial=True)
    return tuple(figures)


def _paragraph_blocks(paragraph: Paragraph, context: _DocxContext) -> tuple[ContentBlock, ...]:
    from docx.oxml.ns import qn
    from docx.text.run import Run

    spans: list[TextSpan] = []
    blocks: list[ContentBlock] = []

    def flush() -> None:
        block = _text_block(spans, paragraph, context)
        if block is not None:
            blocks.append(block)
        spans.clear()

    def consume_run(element: Any, href: str | None = None) -> None:
        run = Run(element, paragraph)
        span = _run_span(run, href, context)
        if span is not None:
            spans.append(span)
        figures = _run_figures(run, context)
        if figures:
            flush()
            blocks.extend(figures)
        page_breaks = cast(list[object], run._r.xpath(".//w:br[@w:type='page']"))
        if page_breaks:
            flush()
            block_id = context.next_id("page-break")
            blocks.append(PageBreakBlock(block_id=block_id, source=context.location()))

    def consume(parent: Any) -> None:
        for child in parent:
            if child.tag == qn("w:r"):
                consume_run(child)
            elif child.tag == qn("w:hyperlink"):
                relationship_id = child.get(qn("r:id"))
                anchor = child.get(qn("w:anchor"))
                href = f"#{anchor}" if anchor else None
                if relationship_id:
                    relationship = paragraph.part.rels.get(relationship_id)
                    if relationship is not None:
                        href = str(relationship.target_ref)
                for nested in child:
                    if nested.tag == qn("w:r"):
                        consume_run(nested, href)
            elif child.tag == qn("w:ins"):
                consume(child)
            elif child.tag == qn("w:del"):
                continue

    consume(paragraph._p)
    flush()
    return tuple(blocks)


def _table_block(table: Table, context: _DocxContext) -> TableBlock:
    rows = tuple(table.rows)
    row_count = max(1, len(rows))
    column_count = max((len(row.cells) for row in rows), default=1)
    positions: dict[int, tuple[_Cell, list[tuple[int, int]]]] = {}
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row.cells):
            key = id(cell._tc)
            if key not in positions:
                positions[key] = (cell, [])
            positions[key][1].append((row_index, column_index))

    cells_by_row: dict[int, list[TableCell]] = {index: [] for index in range(row_count)}
    for cell, cell_positions in positions.values():
        origin_row = min(row for row, _ in cell_positions)
        origin_column = min(column for _, column in cell_positions)
        row_span = max(row for row, _ in cell_positions) - origin_row + 1
        column_span = max(column for _, column in cell_positions) - origin_column + 1
        cell_blocks = _story_blocks(tuple(cell.iter_inner_content()), context)
        displayed = normalize_text(cell.text).strip()
        cells_by_row[origin_row].append(
            TableCell(
                column_index=origin_column,
                row_span=row_span,
                column_span=column_span,
                is_header=origin_row == 0 and len(rows) > 1,
                raw_value=displayed,
                displayed_text=displayed,
                blocks=cell_blocks,
            )
        )

    block_id = context.next_id("table")
    return TableBlock(
        block_id=block_id,
        row_count=row_count,
        column_count=max(1, column_count),
        rows=tuple(
            TableRow(
                row_index=index,
                cells=tuple(sorted(cells_by_row[index], key=lambda c: c.column_index)),
            )
            for index in range(row_count)
        ),
        source=context.location(),
        attributes={"style": table.style.name if table.style else ""},
    )


def _build_lists(entries: tuple[_ListEntry, ...], context: _DocxContext) -> tuple[ListBlock, ...]:
    def consume(index: int, level: int, kind: ListKind) -> tuple[ListBlock, int]:
        items: list[ListItem] = []
        start = entries[index].spec.start
        while index < len(entries):
            entry = entries[index]
            if entry.spec.level < level or entry.spec.kind is not kind:
                break
            if entry.spec.level > level:
                nested, index = consume(index, entry.spec.level, entry.spec.kind)
                previous = items[-1]
                items[-1] = ListItem(blocks=(*previous.blocks, nested))
                continue
            items.append(ListItem(blocks=entry.blocks))
            index += 1
        block_id = context.next_id("list")
        return (
            ListBlock(
                block_id=block_id,
                kind=kind,
                start=start if kind is ListKind.ORDERED else None,
                items=tuple(items),
                source=context.location(),
                attributes={"level": level},
            ),
            index,
        )

    result: list[ListBlock] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        block, index = consume(index, entry.spec.level, entry.spec.kind)
        result.append(block)
    return tuple(result)


def _story_blocks(items: tuple[object, ...], context: _DocxContext) -> tuple[ContentBlock, ...]:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    blocks: list[ContentBlock] = []
    pending_lists: list[_ListEntry] = []

    def flush_lists() -> None:
        if pending_lists:
            blocks.extend(_build_lists(tuple(pending_lists), context))
            pending_lists.clear()

    for item in items:
        if isinstance(item, Paragraph):
            paragraph_blocks = _paragraph_blocks(item, context)
            spec = _list_spec(item, context)
            if spec is not None and paragraph_blocks:
                pending_lists.append(_ListEntry(spec=spec, blocks=paragraph_blocks))
            else:
                flush_lists()
                blocks.extend(paragraph_blocks)
        elif isinstance(item, Table):
            flush_lists()
            blocks.append(_table_block(item, context))
    flush_lists()
    return tuple(blocks)


def _section_stories(
    section: Section,
    section_index: int,
    context: _DocxContext,
    seen: set[str],
) -> tuple[ContainerBlock, ...]:
    stories: list[ContainerBlock] = []
    variants = (
        ("header", "default", section.header, context.options.docx.include_headers),
        ("header", "first", section.first_page_header, context.options.docx.include_headers),
        ("header", "even", section.even_page_header, context.options.docx.include_headers),
        ("footer", "default", section.footer, context.options.docx.include_footers),
        ("footer", "first", section.first_page_footer, context.options.docx.include_footers),
        ("footer", "even", section.even_page_footer, context.options.docx.include_footers),
    )
    for story_name, variant, story, enabled in variants:
        if not enabled:
            continue
        part_name = str(story.part.partname)
        if part_name in seen:
            continue
        seen.add(part_name)
        blocks = _story_blocks(tuple(story.iter_inner_content()), context)
        if not blocks:
            continue
        block_id = context.next_id(story_name)
        stories.append(
            ContainerBlock(
                block_id=block_id,
                role=ContainerRole.SECTION,
                title=(TextSpan(text=story_name.title()),),
                source=context.location(),
                attributes={
                    "section_index": section_index,
                    "story": story_name,
                    "variant": variant,
                },
                blocks=blocks,
            )
        )
    return tuple(stories)


def _note_stories(data: bytes, context: _DocxContext) -> tuple[ContainerBlock, ...]:
    from docx.oxml.ns import qn
    from lxml import etree

    requested = (
        ("footnotes", "word/footnotes.xml", context.options.docx.include_footnotes),
        ("endnotes", "word/endnotes.xml", context.options.docx.include_endnotes),
    )
    result: list[ContainerBlock] = []
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        for story_name, path, enabled in requested:
            if not enabled or path not in names:
                continue
            try:
                root = etree.fromstring(archive.read(path), parser=parser)
            except (etree.XMLSyntaxError, OSError, KeyError) as exc:
                raise InvalidDocumentError(f"could not parse {story_name}") from exc
            blocks: list[ContentBlock] = []
            note_tag = qn("w:footnote" if story_name == "footnotes" else "w:endnote")
            for note in root.findall(note_tag):
                raw_id = note.get(qn("w:id"), "0")
                if int(raw_id) < 0:
                    continue
                text = normalize_text("".join(note.itertext())).strip()
                if text:
                    block_id = context.next_id(story_name[:-1])
                    blocks.append(
                        ParagraphBlock(
                            block_id=block_id,
                            spans=(TextSpan(text=text),),
                            source=context.location(),
                            attributes={"note_id": raw_id},
                        )
                    )
            if blocks:
                block_id = context.next_id(story_name)
                result.append(
                    ContainerBlock(
                        block_id=block_id,
                        role=ContainerRole.SECTION,
                        title=(TextSpan(text=story_name.title()),),
                        source=context.location(),
                        attributes={"story": story_name},
                        blocks=tuple(blocks),
                    )
                )
    return tuple(result)


def _contains_deleted_revision(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return b"<w:del" in archive.read("word/document.xml")
    except (KeyError, OSError, zipfile.BadZipFile):
        return False
