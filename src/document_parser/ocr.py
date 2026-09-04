"""Engine-neutral OCR contracts and selective PDF OCR orchestration."""

from __future__ import annotations

import io
import math
import re
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from document_parser.exceptions import (
    OcrConfigurationError,
    OcrDependencyNotAvailableError,
    OcrExecutionError,
    OcrModelNotAvailableError,
    UnsafeDocumentError,
)
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
    DocumentStatus,
    FigureBlock,
    FrozenModel,
    HeadingBlock,
    ListBlock,
    ListItem,
    ListKind,
    ParagraphBlock,
    SourceLocation,
    TableBlock,
    TableCell,
    TableRow,
    TextSpan,
)
from document_parser.results import AdapterOutput

if TYPE_CHECKING:
    from document_parser.sources import AdapterInput, ParseOptions


class OcrMode(StrEnum):
    """When OCR should run for PDF pages."""

    OFF = "off"
    AUTO = "auto"
    FORCE = "force"


class OcrProfile(StrEnum):
    """Accuracy/structure trade-off exposed by an OCR engine."""

    STRUCTURED = "structured"
    TEXT = "text"


class OcrDevice(StrEnum):
    """Inference device selected explicitly for reproducible behavior."""

    CPU = "cpu"
    GPU = "gpu"


class OcrRegionKind(StrEnum):
    """Engine-neutral semantic labels mapped into the document IR."""

    DOCUMENT_TITLE = "document_title"
    PARAGRAPH_TITLE = "paragraph_title"
    TEXT = "text"
    LIST = "list"
    TABLE = "table"
    FIGURE = "figure"
    CAPTION = "caption"


class OcrOptions(FrozenModel):
    """Selective local-OCR behavior and resource limits."""

    mode: OcrMode = OcrMode.OFF
    profile: OcrProfile = OcrProfile.STRUCTURED
    languages: tuple[str, ...] = ("az", "en", "ru")
    model_store: Path | None = None
    device: OcrDevice = OcrDevice.CPU
    dpi: int = Field(default=300, ge=72, le=600)
    max_pages: int = Field(default=200, gt=0)
    max_page_pixels: int = Field(default=40_000_000, gt=0)
    max_total_pixels: int = Field(default=500_000_000, gt=0)
    min_region_confidence: float = Field(default=0.50, ge=0, le=1)
    min_page_confidence: float = Field(default=0.75, ge=0, le=1)
    max_low_confidence_fraction: float = Field(default=0.20, ge=0, le=1)
    use_orientation: bool = True
    use_unwarping: bool = True

    @field_validator("languages")
    @classmethod
    def validate_languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip().lower() for item in value)
        if not normalized or any(not item or not item.isalpha() for item in normalized):
            raise ValueError("languages must contain non-empty alphabetic language codes")
        if len(normalized) != len(set(normalized)):
            raise ValueError("languages must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_pixel_limits(self) -> OcrOptions:
        if self.max_page_pixels > self.max_total_pixels:
            raise ValueError("max_page_pixels cannot exceed max_total_pixels")
        return self


def _require_pixel_box(value: BoundingBox) -> BoundingBox:
    if value.unit is not CoordinateUnit.PIXEL:
        raise ValueError("OCR bounding boxes must use pixel coordinates")
    return value


class OcrTextLine(FrozenModel):
    """One recognized line with image-space provenance."""

    text: str = Field(min_length=1)
    bounding_box: BoundingBox
    confidence: float = Field(ge=0, le=1)
    language: str | None = None

    _pixel_box = field_validator("bounding_box")(_require_pixel_box)


class OcrTableCell(FrozenModel):
    """One merge-aware cell emitted by a structured OCR engine."""

    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    text: str = ""
    bounding_box: BoundingBox | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("bounding_box")
    @classmethod
    def validate_optional_pixel_box(cls, value: BoundingBox | None) -> BoundingBox | None:
        return None if value is None else _require_pixel_box(value)


class OcrTable(FrozenModel):
    """Rectangular OCR table geometry before conversion into IR cells."""

    row_count: int = Field(ge=1)
    column_count: int = Field(ge=1)
    cells: tuple[OcrTableCell, ...]

    @model_validator(mode="after")
    def validate_cells(self) -> OcrTable:
        occupied: set[tuple[int, int]] = set()
        positions = tuple((cell.row_index, cell.column_index) for cell in self.cells)
        if positions != tuple(sorted(set(positions))):
            raise ValueError("OCR table cells must have unique ordered positions")
        for cell in self.cells:
            if cell.row_index + cell.row_span > self.row_count:
                raise ValueError("OCR table cell exceeds row_count")
            if cell.column_index + cell.column_span > self.column_count:
                raise ValueError("OCR table cell exceeds column_count")
            covered = {
                (row, column)
                for row in range(cell.row_index, cell.row_index + cell.row_span)
                for column in range(cell.column_index, cell.column_index + cell.column_span)
            }
            if occupied.intersection(covered):
                raise ValueError("OCR table cells cannot overlap")
            occupied.update(covered)
        return self


class OcrRegion(FrozenModel):
    """A semantic page region ordered for downstream reading."""

    order: int = Field(ge=0)
    kind: OcrRegionKind
    bounding_box: BoundingBox
    confidence: float = Field(ge=0, le=1)
    lines: tuple[OcrTextLine, ...] = ()
    table: OcrTable | None = None

    _pixel_box = field_validator("bounding_box")(_require_pixel_box)

    @model_validator(mode="after")
    def validate_payload(self) -> OcrRegion:
        if self.kind is OcrRegionKind.TABLE and self.table is None:
            raise ValueError("table OCR regions require table data")
        if self.kind is not OcrRegionKind.TABLE and self.table is not None:
            raise ValueError("only table OCR regions may contain table data")
        if self.kind not in {OcrRegionKind.FIGURE, OcrRegionKind.TABLE} and not self.lines:
            raise ValueError("textual OCR regions require at least one line")
        return self

    @property
    def text(self) -> str:
        """Return normalized region text without engine-specific markup."""

        return " ".join(line.text.strip() for line in self.lines if line.text.strip())


class OcrEngineDiagnostic(FrozenModel):
    """Non-fatal page information returned by an OCR engine."""

    code: str = Field(pattern=r"^ocr\.[a-z0-9_.-]+$")
    message: str = Field(min_length=1)
    severity: DiagnosticSeverity = DiagnosticSeverity.WARNING


class OcrPageInput(FrozenModel):
    """A bounded PNG rendering passed to an OCR engine."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    page_number: int = Field(ge=1)
    image_png: bytes = Field(min_length=1, repr=False)
    width_pixels: int = Field(gt=0)
    height_pixels: int = Field(gt=0)
    source_width_points: float = Field(gt=0)
    source_height_points: float = Field(gt=0)
    dpi: int = Field(gt=0)
    rotation: int = Field(default=0)

    @field_validator("rotation")
    @classmethod
    def validate_rotation(cls, value: int) -> int:
        normalized = value % 360
        if normalized not in {0, 90, 180, 270}:
            raise ValueError("rotation must be a multiple of 90 degrees")
        return normalized


class OcrPageResult(FrozenModel):
    """Validated result returned for one rendered page."""

    page_number: int = Field(ge=1)
    regions: tuple[OcrRegion, ...] = ()
    engine: str = Field(min_length=1)
    models: tuple[str, ...] = ()
    diagnostics: tuple[OcrEngineDiagnostic, ...] = ()
    ambiguous_language: bool = False

    @model_validator(mode="after")
    def validate_order(self) -> OcrPageResult:
        order = tuple(region.order for region in self.regions)
        if order != tuple(sorted(set(order))):
            raise ValueError("OCR regions must have unique increasing order values")
        return self


@runtime_checkable
class OcrEngine(Protocol):
    """Contract implemented by local OCR backends."""

    name: str

    def recognize(self, page: OcrPageInput, options: OcrOptions) -> OcrPageResult:
        """Recognize one already-rendered PDF page."""


def _page_number(page: ContainerBlock) -> int | None:
    return page.source.page_number if page.source else None


def _select_pages(document: Document, options: OcrOptions) -> tuple[ContainerBlock, ...]:
    pages = tuple(
        block
        for block in document.blocks
        if isinstance(block, ContainerBlock)
        and block.role is ContainerRole.PAGE
        and _page_number(block) is not None
    )
    if options.mode is OcrMode.AUTO:
        return tuple(page for page in pages if page.attributes.get("scan_candidate") is True)
    if options.mode is OcrMode.FORCE:
        return tuple(page for page in pages if page.blocks)
    return ()


def _render_page(
    pdf: object,
    page_number: int,
    rotation: int,
    options: OcrOptions,
) -> OcrPageInput:
    try:
        page = pdf[page_number - 1]  # type: ignore[index]
        width_points, height_points = page.get_size()
        scale = options.dpi / 72
        expected_width = max(1, math.ceil(float(width_points) * scale))
        expected_height = max(1, math.ceil(float(height_points) * scale))
        expected_pixels = expected_width * expected_height
        if expected_pixels > options.max_page_pixels:
            raise UnsafeDocumentError(f"OCR page exceeds max_page_pixels={options.max_page_pixels}")
        bitmap = page.render(scale=scale, rotation=0, rev_byteorder=True)
        image = bitmap.to_pil().convert("RGBA")
        from PIL import Image

        flattened = Image.new("RGB", image.size, "white")
        flattened.paste(image, mask=image.getchannel("A"))
        buffer = io.BytesIO()
        flattened.save(buffer, format="PNG", optimize=False)
        width_pixels, height_pixels = flattened.size
        return OcrPageInput(
            page_number=page_number,
            image_png=buffer.getvalue(),
            width_pixels=width_pixels,
            height_pixels=height_pixels,
            source_width_points=float(width_points),
            source_height_points=float(height_points),
            dpi=options.dpi,
            rotation=rotation,
        )
    except UnsafeDocumentError:
        raise
    except Exception as exc:
        raise OcrExecutionError(f"PDF page {page_number} could not be rendered") from exc
    finally:
        for resource_name in ("bitmap", "page"):
            resource = locals().get(resource_name)
            close = getattr(resource, "close", None)
            if callable(close):
                close()


def _point_box(box: BoundingBox, page: OcrPageInput) -> BoundingBox:
    scale_x = page.source_width_points / box.canvas_width
    scale_y = page.source_height_points / box.canvas_height
    return BoundingBox(
        x=box.x * scale_x,
        y=box.y * scale_y,
        width=box.width * scale_x,
        height=box.height * scale_y,
        canvas_width=page.source_width_points,
        canvas_height=page.source_height_points,
        unit=CoordinateUnit.POINT,
    )


def _location(
    page: OcrPageInput,
    box: BoundingBox,
    confidence: float,
    block_index: int,
) -> SourceLocation:
    return SourceLocation(
        page_number=page.page_number,
        block_index=block_index,
        bounding_box=_point_box(box, page),
        confidence=confidence,
    )


def _attributes(result: OcrPageResult, options: OcrOptions) -> dict[str, JsonValue]:
    return {
        "active_for_rag": True,
        "extraction_method": "ocr",
        "ocr_engine": result.engine,
        "ocr_models": list(result.models),
        "ocr_profile": options.profile.value,
    }


def _table_block(
    region: OcrRegion,
    page: OcrPageInput,
    result: OcrPageResult,
    options: OcrOptions,
) -> TableBlock:
    assert region.table is not None
    table = region.table
    rows: dict[int, list[TableCell]] = {index: [] for index in range(table.row_count)}
    for cell_index, cell in enumerate(table.cells):
        nested: tuple[ContentBlock, ...] = ()
        if cell.text:
            cell_box = cell.bounding_box or region.bounding_box
            cell_confidence = cell.confidence if cell.confidence is not None else region.confidence
            nested = (
                ParagraphBlock(
                    block_id=(
                        f"pdf:ocr:p{page.page_number:04d}:table:{region.order:04d}:"
                        f"cell:{cell_index:04d}"
                    ),
                    spans=(TextSpan(text=cell.text),),
                    source=_location(page, cell_box, cell_confidence, region.order),
                    attributes=_attributes(result, options),
                ),
            )
        rows[cell.row_index].append(
            TableCell(
                column_index=cell.column_index,
                row_span=cell.row_span,
                column_span=cell.column_span,
                is_header=cell.row_index == 0,
                raw_value=cell.text,
                displayed_text=cell.text,
                blocks=nested,
            )
        )
    return TableBlock(
        block_id=f"pdf:ocr:p{page.page_number:04d}:table:{region.order:04d}",
        row_count=table.row_count,
        column_count=table.column_count,
        rows=tuple(
            TableRow(
                row_index=index,
                cells=tuple(sorted(rows[index], key=lambda item: item.column_index)),
            )
            for index in range(table.row_count)
        ),
        source=_location(page, region.bounding_box, region.confidence, region.order),
        attributes=_attributes(result, options),
    )


_ORDERED_ITEM = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_UNORDERED_ITEM = re.compile(r"^\s*[-*•]\s+(.*)$")


def _list_block(
    region: OcrRegion,
    page: OcrPageInput,
    result: OcrPageResult,
    options: OcrOptions,
) -> ListBlock:
    ordered_matches = tuple(_ORDERED_ITEM.match(line.text) for line in region.lines)
    ordered = all(match is not None for match in ordered_matches)
    items: list[ListItem] = []
    for index, line in enumerate(region.lines):
        ordered_match = ordered_matches[index]
        unordered_match = _UNORDERED_ITEM.match(line.text)
        text = (
            ordered_match.group(2)
            if ordered_match is not None
            else unordered_match.group(1)
            if unordered_match is not None
            else line.text
        )
        items.append(
            ListItem(
                blocks=(
                    ParagraphBlock(
                        block_id=(
                            f"pdf:ocr:p{page.page_number:04d}:list:{region.order:04d}:"
                            f"item:{index:04d}"
                        ),
                        spans=(TextSpan(text=text),),
                        source=_location(page, line.bounding_box, line.confidence, region.order),
                        attributes=_attributes(result, options),
                    ),
                )
            )
        )
    first = ordered_matches[0] if ordered_matches else None
    return ListBlock(
        block_id=f"pdf:ocr:p{page.page_number:04d}:list:{region.order:04d}",
        kind=ListKind.ORDERED if ordered else ListKind.UNORDERED,
        start=int(first.group(1)) if first is not None else None,
        items=tuple(items),
        source=_location(page, region.bounding_box, region.confidence, region.order),
        attributes=_attributes(result, options),
    )


def _region_block(
    region: OcrRegion,
    page: OcrPageInput,
    result: OcrPageResult,
    options: OcrOptions,
) -> ContentBlock | None:
    if region.kind is OcrRegionKind.FIGURE:
        return None
    if region.kind is OcrRegionKind.TABLE:
        return _table_block(region, page, result, options)
    if region.kind is OcrRegionKind.LIST:
        return _list_block(region, page, result, options)
    block_id = f"pdf:ocr:p{page.page_number:04d}:{region.kind.value}:{region.order:04d}"
    source = _location(page, region.bounding_box, region.confidence, region.order)
    spans = (TextSpan(text=region.text),)
    attributes = _attributes(result, options)
    if region.kind is OcrRegionKind.DOCUMENT_TITLE:
        return HeadingBlock(
            block_id=block_id,
            level=1,
            spans=spans,
            source=source,
            attributes=attributes,
        )
    if region.kind is OcrRegionKind.PARAGRAPH_TITLE:
        return HeadingBlock(
            block_id=block_id,
            level=2,
            spans=spans,
            source=source,
            attributes=attributes,
        )
    return ParagraphBlock(
        block_id=block_id,
        spans=spans,
        source=source,
        attributes=attributes,
    )


def _deactivate_native(block: ContentBlock) -> ContentBlock:
    if isinstance(block, ContainerBlock):
        block = block.model_copy(
            update={"blocks": tuple(_deactivate_native(child) for child in block.blocks)}
        )
    elif isinstance(block, ListBlock):
        block = block.model_copy(
            update={
                "items": tuple(
                    item.model_copy(
                        update={"blocks": tuple(_deactivate_native(child) for child in item.blocks)}
                    )
                    for item in block.items
                )
            }
        )
    elif isinstance(block, TableBlock):
        block = block.model_copy(
            update={
                "rows": tuple(
                    row.model_copy(
                        update={
                            "cells": tuple(
                                cell.model_copy(
                                    update={
                                        "blocks": tuple(
                                            _deactivate_native(child) for child in cell.blocks
                                        )
                                    }
                                )
                                for cell in row.cells
                            )
                        }
                    )
                    for row in block.rows
                )
            }
        )
    attributes = {
        **block.attributes,
        "active_for_rag": False,
        "extraction_method": "native",
    }
    return block.model_copy(update={"attributes": attributes})


def _is_page_background(block: FigureBlock) -> bool:
    if block.source is None or block.source.bounding_box is None:
        return False
    box = block.source.bounding_box
    return box.width * box.height >= box.canvas_width * box.canvas_height * 0.5


def _confidence(result: OcrPageResult, options: OcrOptions) -> tuple[float, float]:
    weighted = 0.0
    characters = 0
    low_characters = 0
    for region in result.regions:
        if region.kind is OcrRegionKind.TABLE and region.table is not None:
            values = tuple(
                (cell.text, cell.confidence or region.confidence) for cell in region.table.cells
            )
        else:
            values = tuple((line.text, line.confidence) for line in region.lines)
        for text, confidence in values:
            count = max(1, len(text.strip()))
            weighted += confidence * count
            characters += count
            if confidence < options.min_region_confidence:
                low_characters += count
    if characters == 0:
        return 0.0, 1.0
    return weighted / characters, low_characters / characters


def _diagnostic(
    code: str,
    message: str,
    page_number: int,
    *,
    severity: DiagnosticSeverity = DiagnosticSeverity.WARNING,
    details: dict[str, JsonValue] | None = None,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        message=message,
        severity=severity,
        location=SourceLocation(page_number=page_number),
        details=details or {},
    )


def apply_pdf_ocr(
    source: AdapterInput,
    output: AdapterOutput,
    options: ParseOptions,
    engine: OcrEngine | None = None,
) -> tuple[AdapterOutput, OcrEngine | None]:
    """Apply configured OCR to selected PDF pages and return the reusable engine."""

    ocr_options = options.ocr
    if ocr_options.mode is OcrMode.OFF or output.document.source.format is not DocumentFormat.PDF:
        return output, engine
    selected = _select_pages(output.document, ocr_options)
    if not selected:
        return output, engine
    if len(selected) > ocr_options.max_pages:
        raise UnsafeDocumentError(
            f"OCR selection exceeds max_pages={ocr_options.max_pages}",
            source_name=source.info.name,
        )

    if engine is None:
        from document_parser.paddle_ocr import PaddleOcrEngine

        engine = PaddleOcrEngine(ocr_options, source_name=source.info.name)
    if not isinstance(engine, OcrEngine):
        raise OcrConfigurationError(
            "ocr_engine does not implement the OcrEngine protocol",
            source_name=source.info.name,
        )

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise OcrDependencyNotAvailableError(
            "PDF OCR rendering requires the 'ocr' optional dependencies",
            source_name=source.info.name,
        ) from exc

    with source.open_binary() as stream:
        data = stream.read()
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as exc:
        raise OcrExecutionError(
            "PDF could not be opened by the OCR renderer", source_name=source.info.name
        ) from exc

    selected_numbers = {_page_number(page) for page in selected}
    replacements: dict[int, ContainerBlock] = {}
    diagnostics = list(output.document.diagnostics)
    failed = False
    review = False
    total_pixels = 0
    try:
        for original in selected:
            number = _page_number(original)
            assert number is not None
            raw_rotation = original.attributes.get("rotation", 0)
            rotation = int(raw_rotation) if isinstance(raw_rotation, (str, int, float)) else 0
            try:
                rendered = _render_page(pdf, number, rotation, ocr_options)
                pixels = rendered.width_pixels * rendered.height_pixels
                total_pixels += pixels
                if total_pixels > ocr_options.max_total_pixels:
                    raise UnsafeDocumentError(
                        f"OCR rendering exceeds max_total_pixels={ocr_options.max_total_pixels}",
                        source_name=source.info.name,
                    )
                recognized = engine.recognize(rendered, ocr_options)
                if recognized.page_number != number:
                    raise OcrExecutionError("OCR engine returned a result for a different page")
            except UnsafeDocumentError:
                raise
            except (
                OcrDependencyNotAvailableError,
                OcrModelNotAvailableError,
                OcrConfigurationError,
            ):
                raise
            except Exception:
                failed = True
                diagnostics.append(
                    _diagnostic(
                        "ocr.page_failed",
                        "The selected PDF page could not be OCR processed.",
                        number,
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
                continue

            page_confidence, low_fraction = _confidence(recognized, ocr_options)
            blocks = tuple(
                block
                for region in recognized.regions
                if (block := _region_block(region, rendered, recognized, ocr_options)) is not None
            )
            has_text = any(
                region.text or (region.table and any(cell.text for cell in region.table.cells))
                for region in recognized.regions
            )
            page_review = False
            if not has_text:
                page_review = True
                diagnostics.append(
                    _diagnostic(
                        "ocr.no_text_detected",
                        "OCR did not find text on the selected page.",
                        number,
                    )
                )
            if (
                page_confidence < ocr_options.min_page_confidence
                or low_fraction > ocr_options.max_low_confidence_fraction
            ):
                page_review = True
                diagnostics.append(
                    _diagnostic(
                        "ocr.low_confidence",
                        "OCR confidence is below the configured review threshold.",
                        number,
                        details={
                            "page_confidence": round(page_confidence, 6),
                            "low_confidence_fraction": round(low_fraction, 6),
                        },
                    )
                )
            if recognized.ambiguous_language:
                page_review = True
                diagnostics.append(
                    _diagnostic(
                        "ocr.language_ambiguous",
                        "OCR language selection was ambiguous for this page.",
                        number,
                    )
                )
            for item in recognized.diagnostics:
                diagnostics.append(
                    _diagnostic(
                        item.code,
                        item.message,
                        number,
                        severity=item.severity,
                    )
                )
                page_review = page_review or item.severity is not DiagnosticSeverity.INFO
            review = review or page_review

            native = tuple(
                (
                    _deactivate_native(block)
                    if not isinstance(block, FigureBlock) or _is_page_background(block)
                    else block
                )
                if has_text
                else block
                for block in original.blocks
            )
            attributes = {
                **original.attributes,
                "ocr_applied": True,
                "ocr_profile": ocr_options.profile.value,
                "ocr_engine": recognized.engine,
                "ocr_models": list(recognized.models),
                "ocr_page_confidence": round(page_confidence, 6),
                "text_source": "ocr" if has_text else "native_fallback",
            }
            replacements[number] = original.model_copy(
                update={"attributes": attributes, "blocks": (*blocks, *native)}
            )
            if has_text:
                diagnostics = [
                    item
                    for item in diagnostics
                    if not (
                        item.code == "pdf.ocr_required"
                        and item.location is not None
                        and item.location.page_number == number
                    )
                ]
            diagnostics.append(
                _diagnostic(
                    "ocr.applied",
                    "Local OCR was applied to the selected PDF page.",
                    number,
                    severity=DiagnosticSeverity.INFO,
                    details={
                        "engine": recognized.engine,
                        "models": list(recognized.models),
                        "profile": ocr_options.profile.value,
                    },
                )
            )
    finally:
        close = getattr(pdf, "close", None)
        if callable(close):
            close()

    rebuilt = tuple(
        replacements.get(number, block)
        if isinstance(block, ContainerBlock) and (number := _page_number(block)) is not None
        else block
        for block in output.document.blocks
    )
    unresolved_candidate = any(
        item.code == "pdf.ocr_required"
        and item.location is not None
        and item.location.page_number in selected_numbers
        for item in diagnostics
    )
    if output.document.status is DocumentStatus.PARTIAL or failed:
        status = DocumentStatus.PARTIAL
    elif review or unresolved_candidate:
        status = DocumentStatus.NEEDS_REVIEW
    else:
        status = DocumentStatus.COMPLETE
    document = output.document.model_copy(
        update={"blocks": rebuilt, "diagnostics": tuple(diagnostics), "status": status}
    )
    return AdapterOutput(document=document, assets=output.assets), engine
