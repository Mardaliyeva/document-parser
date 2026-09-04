"""Born-digital PDF adapter with page-level OCR-candidate detection."""

from __future__ import annotations

import io
import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, cast

from document_parser._adapter_utils import AssetCollector, normalize_text
from document_parser.exceptions import InvalidDocumentError, UnsafeDocumentError
from document_parser.models import (
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
    ParagraphBlock,
    SourceLocation,
    TableBlock,
    TableCell,
    TableRow,
    TextSpan,
)
from document_parser.results import AdapterOutput
from document_parser.sources import AdapterInput, ParseOptions


@dataclass(frozen=True, slots=True)
class _PdfWord:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    fontname: str


@dataclass(frozen=True, slots=True)
class _PdfLine:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool


@dataclass(frozen=True, slots=True)
class _PdfCellData:
    bbox: tuple[float, float, float, float]
    text: str


@dataclass(frozen=True, slots=True)
class _PdfTableData:
    bbox: tuple[float, float, float, float]
    cells: tuple[_PdfCellData, ...]


@dataclass(frozen=True, slots=True)
class _PdfPageData:
    page_number: int
    width: float
    height: float
    rotation: int
    words: tuple[_PdfWord, ...]
    lines: tuple[_PdfLine, ...]
    tables: tuple[_PdfTableData, ...]
    image_boxes: tuple[tuple[float, float, float, float], ...]
    scan_candidate: bool


class _PdfContext:
    __slots__ = ("assets", "diagnostics", "needs_review", "partial", "sequence")

    def __init__(self, options: ParseOptions, source_name: str) -> None:
        self.assets = AssetCollector(options, source_name)
        self.diagnostics: list[Diagnostic] = []
        self.needs_review = False
        self.partial = False
        self.sequence = 0

    def next_id(self, kind: str) -> str:
        self.sequence += 1
        return f"pdf:{kind}:{self.sequence:06d}"

    def diagnostic(
        self,
        code: str,
        message: str,
        *,
        page_number: int | None = None,
        severity: DiagnosticSeverity = DiagnosticSeverity.WARNING,
    ) -> None:
        location = SourceLocation(page_number=page_number) if page_number else None
        self.diagnostics.append(
            Diagnostic(code=code, message=message, severity=severity, location=location)
        )


class PdfAdapter:
    """Extract native PDF text, layout, tables, and embedded images."""

    format = DocumentFormat.PDF

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        import pdfplumber
        from pypdf import PdfReader

        with source.open_binary() as stream:
            data = stream.read()
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                raise InvalidDocumentError(
                    "encrypted PDF documents are not supported", source_name=source.info.name
                )
            page_count = len(reader.pages)
        except InvalidDocumentError:
            raise
        except Exception as exc:
            raise InvalidDocumentError(
                "PDF could not be opened", source_name=source.info.name
            ) from exc

        context = _PdfContext(options, source.info.name)
        page_data: list[_PdfPageData] = []
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if len(pdf.pages) != page_count:
                    context.diagnostic(
                        "pdf.page_count_mismatch",
                        "PDF engines reported different page counts.",
                    )
                    context.partial = True
                for page_number, page in enumerate(pdf.pages, start=1):
                    try:
                        page_data.append(_extract_page(page, page_number, options))
                    except Exception:
                        context.diagnostic(
                            "pdf.page_extraction_failed",
                            "A PDF page could not be extracted.",
                            page_number=page_number,
                            severity=DiagnosticSeverity.ERROR,
                        )
                        context.partial = True
        except Exception as exc:
            raise InvalidDocumentError(
                "PDF layout could not be read", source_name=source.info.name
            ) from exc

        repeated = _repeated_margin_lines(
            tuple(page_data), options.pdf.repeated_margin_min_fraction
        )
        body_sizes = [word.size for page in page_data for word in page.words if word.text.strip()]
        body_size = statistics.median(body_sizes) if body_sizes else 10.0
        heading_sizes = sorted(
            {
                round(line.size, 2)
                for page in page_data
                for line in page.lines
                if _heading_candidate(line, body_size) and _line_key(line) not in repeated
            },
            reverse=True,
        )

        pages: list[ContentBlock] = []
        for data_index, extracted in enumerate(page_data):
            blocks = _page_blocks(extracted, repeated, body_size, heading_sizes, context, options)
            if extracted.scan_candidate:
                context.needs_review = True
                context.diagnostic(
                    "pdf.ocr_required",
                    "The page contains too little native text and is an OCR candidate.",
                    page_number=extracted.page_number,
                )
            if data_index < len(reader.pages):
                blocks.extend(_page_images(reader.pages[data_index], extracted, context))
            block_id = context.next_id("page")
            pages.append(
                ContainerBlock(
                    block_id=block_id,
                    role=ContainerRole.PAGE,
                    source=SourceLocation(
                        page_number=extracted.page_number, block_index=context.sequence
                    ),
                    attributes={
                        "rotation": extracted.rotation,
                        "scan_candidate": extracted.scan_candidate,
                    },
                    blocks=tuple(blocks),
                )
            )

        if context.partial:
            status = DocumentStatus.PARTIAL
        elif context.needs_review:
            status = DocumentStatus.NEEDS_REVIEW
        else:
            status = DocumentStatus.COMPLETE
        document = Document(
            document_id=f"sha256:{source.info.sha256}",
            source=source.info,
            metadata=_pdf_metadata(reader),
            blocks=tuple(pages),
            assets=context.assets.refs,
            status=status,
            diagnostics=tuple((*source.info.diagnostics, *context.diagnostics)),
        )
        return AdapterOutput(document=document, assets=context.assets.payloads)


def _number(value: object, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _extract_words(page: Any) -> tuple[_PdfWord, ...]:
    raw_words = cast(
        list[dict[str, object]],
        page.extract_words(
            extra_attrs=["fontname", "size"],
            keep_blank_chars=False,
            use_text_flow=False,
        ),
    )
    words = [
        _PdfWord(
            text=normalize_text(str(word.get("text", ""))),
            x0=_number(word.get("x0")),
            x1=_number(word.get("x1")),
            top=_number(word.get("top")),
            bottom=_number(word.get("bottom")),
            size=max(0.1, _number(word.get("size"), 10.0)),
            fontname=str(word.get("fontname", "")),
        )
        for word in raw_words
        if str(word.get("text", "")).strip()
    ]
    return tuple(sorted(words, key=lambda word: (round(word.top, 2), word.x0)))


def _group_lines(words: tuple[_PdfWord, ...]) -> tuple[_PdfLine, ...]:
    groups: list[list[_PdfWord]] = []
    for word in words:
        if not groups or abs(statistics.fmean(item.top for item in groups[-1]) - word.top) > 3:
            groups.append([word])
        else:
            groups[-1].append(word)
    lines: list[_PdfLine] = []
    for group in groups:
        ordered = sorted(group, key=lambda word: word.x0)
        typical_height = statistics.median(word.bottom - word.top for word in ordered)
        split_threshold = max(24.0, typical_height * 4)
        chunks: list[list[_PdfWord]] = [[]]
        for word in ordered:
            if chunks[-1] and word.x0 - chunks[-1][-1].x1 > split_threshold:
                chunks.append([])
            chunks[-1].append(word)
        for chunk in chunks:
            lines.append(
                _PdfLine(
                    text=" ".join(word.text for word in chunk),
                    x0=min(word.x0 for word in chunk),
                    x1=max(word.x1 for word in chunk),
                    top=min(word.top for word in chunk),
                    bottom=max(word.bottom for word in chunk),
                    size=max(word.size for word in chunk),
                    bold=any("bold" in word.fontname.lower() for word in chunk),
                )
            )
    return tuple(lines)


def _column_split(lines: Sequence[_PdfLine]) -> float | None:
    """Find a stable two-column split from repeated horizontal start positions."""

    if len(lines) < 4:
        return None
    starts = sorted(line.x0 for line in lines)
    gaps = [(right - left, left, right) for left, right in pairwise(starts)]
    gap, left, right = max(gaps)
    minimum_gap = max(60.0, statistics.median(line.size for line in lines) * 6)
    if gap < minimum_gap:
        return None
    split = (left + right) / 2
    if sum(line.x0 < split for line in lines) < 2 or sum(line.x0 >= split for line in lines) < 2:
        return None
    return split


def _image_boxes(
    page: Any, width: float, height: float
) -> tuple[tuple[float, float, float, float], ...]:
    result: list[tuple[float, float, float, float]] = []
    for image in cast(list[dict[str, object]], page.images):
        x0 = max(0.0, min(width, _number(image.get("x0"))))
        x1 = max(x0, min(width, _number(image.get("x1"), x0)))
        top = max(0.0, min(height, _number(image.get("top"))))
        bottom = max(top, min(height, _number(image.get("bottom"), top)))
        if x1 > x0 and bottom > top:
            result.append((x0, top, x1, bottom))
    return tuple(result)


def _materialize_table(raw_table: Any, page: Any) -> _PdfTableData:
    cells: list[_PdfCellData] = []
    for raw_cell in cast(list[tuple[float, ...]], raw_table.cells):
        box = cast(tuple[float, float, float, float], tuple(map(float, raw_cell)))
        text = normalize_text(str(page.crop(box).extract_text() or "")).strip()
        cells.append(_PdfCellData(bbox=box, text=text))
    return _PdfTableData(
        bbox=cast(tuple[float, float, float, float], tuple(map(float, raw_table.bbox))),
        cells=tuple(cells),
    )


def _find_tables(page: Any, options: ParseOptions) -> tuple[_PdfTableData, ...]:
    if not options.pdf.detect_tables:
        return ()
    line_settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
    tables = tuple(cast(list[object], page.find_tables(table_settings=line_settings)))
    if tables:
        return tuple(_materialize_table(table, page) for table in tables)
    words = _extract_words(page)
    if len(_group_lines(words)) < 3:
        return ()
    text_settings = {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "min_words_vertical": 2,
        "min_words_horizontal": 1,
    }
    candidates = tuple(cast(list[object], page.find_tables(table_settings=text_settings)))
    accepted = tuple(
        table
        for table in candidates
        if len(getattr(table, "rows", ())) >= 3 and len(getattr(table, "columns", ())) >= 2
    )
    return tuple(_materialize_table(table, page) for table in accepted)


def _extract_page(page: Any, page_number: int, options: ParseOptions) -> _PdfPageData:
    width = max(0.1, float(page.width))
    height = max(0.1, float(page.height))
    words = _extract_words(page)
    image_boxes = _image_boxes(page, width, height)
    image_area = min(
        width * height,
        sum((x1 - x0) * (bottom - top) for x0, top, x1, bottom in image_boxes),
    )
    native_count = sum(character.isalnum() for word in words for character in word.text)
    image_coverage = image_area / (width * height)
    scan_candidate = (
        native_count < options.pdf.min_native_alphanumeric_chars
        and image_coverage >= options.pdf.scan_image_coverage_threshold
    )
    return _PdfPageData(
        page_number=page_number,
        width=width,
        height=height,
        rotation=int(getattr(page, "rotation", 0) or 0),
        words=words,
        lines=_group_lines(words),
        tables=_find_tables(page, options),
        image_boxes=image_boxes,
        scan_candidate=scan_candidate,
    )


def _line_key(line: _PdfLine) -> str:
    return re.sub(r"\s+", " ", line.text).strip().casefold()


def _repeated_margin_lines(pages: tuple[_PdfPageData, ...], minimum_fraction: float) -> set[str]:
    if len(pages) < 2:
        return set()
    occurrences: dict[str, set[int]] = {}
    for page in pages:
        for line in page.lines:
            if line.top <= page.height * 0.1 or line.bottom >= page.height * 0.9:
                occurrences.setdefault(_line_key(line), set()).add(page.page_number)
    threshold = max(2, math.ceil(len(pages) * minimum_fraction))
    return {
        key for key, page_numbers in occurrences.items() if key and len(page_numbers) >= threshold
    }


def _heading_candidate(line: _PdfLine, body_size: float) -> bool:
    return len(line.text) <= 120 and (
        line.size >= body_size * 1.25 or (line.bold and line.size >= body_size * 1.15)
    )


def _bbox(
    x0: float, top: float, x1: float, bottom: float, width: float, height: float
) -> BoundingBox:
    safe_x0 = max(0.0, min(width - 1e-6, x0))
    safe_top = max(0.0, min(height - 1e-6, top))
    safe_x1 = max(safe_x0 + 1e-6, min(width, x1))
    safe_bottom = max(safe_top + 1e-6, min(height, bottom))
    return BoundingBox(
        x=safe_x0,
        y=safe_top,
        width=min(width - safe_x0, safe_x1 - safe_x0),
        height=min(height - safe_top, safe_bottom - safe_top),
        canvas_width=width,
        canvas_height=height,
        unit=CoordinateUnit.POINT,
    )


def _inside(word: _PdfWord, box: tuple[float, float, float, float]) -> bool:
    center_x = (word.x0 + word.x1) / 2
    center_y = (word.top + word.bottom) / 2
    x0, top, x1, bottom = box
    return x0 <= center_x <= x1 and top <= center_y <= bottom


def _pdf_table(
    raw_table: _PdfTableData,
    page: _PdfPageData,
    context: _PdfContext,
) -> TableBlock | None:
    cells = [cell.bbox for cell in raw_table.cells]
    if not cells:
        return None
    x_values = sorted({round(value, 3) for cell in cells for value in (cell[0], cell[2])})
    y_values = sorted({round(value, 3) for cell in cells for value in (cell[1], cell[3])})
    if len(x_values) < 2 or len(y_values) < 2:
        return None
    rows: dict[int, list[TableCell]] = {index: [] for index in range(len(y_values) - 1)}
    ordered_cells = sorted(raw_table.cells, key=lambda cell: (cell.bbox[1], cell.bbox[0]))
    for cell_data in ordered_cells:
        x0, top, x1, bottom = cell_data.bbox
        column = x_values.index(round(x0, 3))
        row = y_values.index(round(top, 3))
        column_span = x_values.index(round(x1, 3)) - column
        row_span = y_values.index(round(bottom, 3)) - row
        rows[row].append(
            TableCell(
                column_index=column,
                column_span=max(1, column_span),
                row_span=max(1, row_span),
                is_header=row == 0,
                raw_value=cell_data.text,
                displayed_text=cell_data.text,
            )
        )
    table_box = raw_table.bbox
    block_id = context.next_id("table")
    return TableBlock(
        block_id=block_id,
        row_count=len(y_values) - 1,
        column_count=len(x_values) - 1,
        rows=tuple(
            TableRow(
                row_index=index, cells=tuple(sorted(rows[index], key=lambda c: c.column_index))
            )
            for index in range(len(y_values) - 1)
        ),
        source=SourceLocation(
            page_number=page.page_number,
            block_index=context.sequence,
            bounding_box=_bbox(*table_box, page.width, page.height),
        ),
    )


def _page_blocks(
    page: _PdfPageData,
    repeated: set[str],
    body_size: float,
    heading_sizes: list[float],
    context: _PdfContext,
    options: ParseOptions,
) -> list[ContentBlock]:
    table_boxes = [table.bbox for table in page.tables]
    free_lines = [
        line
        for line in page.lines
        if not any(
            _inside(
                _PdfWord(line.text, line.x0, line.x1, line.top, line.bottom, line.size, ""),
                box,
            )
            for box in table_boxes
        )
    ]
    column_split = _column_split(free_lines)
    events: list[tuple[int, float, float, ContentBlock]] = []
    for line in free_lines:
        repeated_margin = _line_key(line) in repeated
        attributes: dict[str, object] = {}
        if repeated_margin:
            attributes["story"] = "header" if line.top <= page.height * 0.1 else "footer"
            attributes["repeated_margin"] = True
        location = SourceLocation(
            page_number=page.page_number,
            block_index=context.sequence + 1,
            bounding_box=_bbox(line.x0, line.top, line.x1, line.bottom, page.width, page.height),
        )
        if (
            options.pdf.infer_headings
            and _heading_candidate(line, body_size)
            and not repeated_margin
        ):
            rounded_size = round(line.size, 2)
            level = (
                min(6, heading_sizes.index(rounded_size) + 1)
                if rounded_size in heading_sizes
                else 6
            )
            block_id = context.next_id("heading")
            block: ContentBlock = HeadingBlock(
                block_id=block_id,
                level=level,
                spans=(TextSpan(text=line.text, bold=line.bold),),
                source=location,
                attributes=cast(dict[str, Any], attributes),
            )
        else:
            block_id = context.next_id("paragraph")
            block = ParagraphBlock(
                block_id=block_id,
                spans=(TextSpan(text=line.text, bold=line.bold),),
                source=location,
                attributes=cast(dict[str, Any], attributes),
            )
        column = 1 if column_split is not None and line.x0 >= column_split else 0
        events.append((column, line.top, line.x0, block))

    for raw_table in page.tables:
        table = _pdf_table(raw_table, page, context)
        if table is not None:
            column = 1 if column_split is not None and raw_table.bbox[0] >= column_split else 0
            events.append((column, raw_table.bbox[1], raw_table.bbox[0], table))
    return [event[3] for event in sorted(events, key=lambda event: event[:3])]


def _page_images(
    page: Any, extracted: _PdfPageData, context: _PdfContext
) -> tuple[FigureBlock, ...]:
    figures: list[FigureBlock] = []
    try:
        images = tuple(page.images)
    except Exception:
        context.diagnostic(
            "pdf.image_extraction_failed",
            "Embedded PDF images could not be extracted.",
            page_number=extracted.page_number,
        )
        context.partial = True
        return ()
    for index, image in enumerate(images):
        try:
            data = bytes(image.data)
            name = str(image.name or f"page-{extracted.page_number}-image-{index + 1}.bin")
            ref = context.assets.add(data, filename=name)
        except UnsafeDocumentError:
            raise
        except Exception:
            context.diagnostic(
                "pdf.image_extraction_failed",
                "An embedded PDF image could not be extracted.",
                page_number=extracted.page_number,
            )
            context.partial = True
            continue
        location = SourceLocation(page_number=extracted.page_number, asset_id=ref.asset_id)
        if index < len(extracted.image_boxes):
            location = location.model_copy(
                update={
                    "bounding_box": _bbox(
                        *extracted.image_boxes[index], extracted.width, extracted.height
                    )
                }
            )
        block_id = context.next_id("figure")
        figures.append(
            FigureBlock(
                block_id=block_id,
                asset_id=ref.asset_id,
                source=location,
                attributes={"image_index": index},
            )
        )
    return tuple(figures)


def _pdf_date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return None


def _pdf_metadata(reader: Any) -> DocumentMetadata:
    metadata = reader.metadata
    if metadata is None:
        return DocumentMetadata()
    author = normalize_text(str(metadata.author)) if metadata.author else None
    return DocumentMetadata(
        title=normalize_text(str(metadata.title)) if metadata.title else None,
        authors=(author,) if author else (),
        subject=normalize_text(str(metadata.subject)) if metadata.subject else None,
        created_at=_pdf_date(getattr(metadata, "creation_date", None)),
        modified_at=_pdf_date(getattr(metadata, "modification_date", None)),
        custom={"producer": str(metadata.producer)} if metadata.producer else {},
    )
