"""Format-independent, validated document intermediate representation."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

SCHEMA_VERSION = "0.1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
DOCUMENT_ID_PATTERN = r"^sha256:[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"
CELL_RANGE_PATTERN = re.compile(
    r"^\$?[A-Za-z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)?$"
)

JSONScalar: TypeAlias = str | int | float | bool | None


class FrozenModel(BaseModel):
    """Shared strict and immutable model configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DocumentFormat(StrEnum):
    """Source formats recognized by preflight detection."""

    DOCX = "docx"
    PDF = "pdf"
    XLSX = "xlsx"


class DocumentStatus(StrEnum):
    """Outcome state for a successfully returned document."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"


class DiagnosticSeverity(StrEnum):
    """Severity assigned to a non-fatal parsing diagnostic."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CoordinateUnit(StrEnum):
    """Units used by a source bounding box."""

    PIXEL = "pixel"
    POINT = "point"


class ContainerRole(StrEnum):
    """Semantic role of a container block."""

    PAGE = "page"
    SECTION = "section"
    SHEET = "sheet"


class ListKind(StrEnum):
    """Supported list marker styles."""

    ORDERED = "ordered"
    UNORDERED = "unordered"


class BoundingBox(FrozenModel):
    """A rectangular region in the coordinate space of a page or image."""

    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    canvas_width: float = Field(gt=0)
    canvas_height: float = Field(gt=0)
    unit: CoordinateUnit

    @model_validator(mode="after")
    def validate_bounds(self) -> BoundingBox:
        tolerance = 1e-9
        if self.x + self.width > self.canvas_width + tolerance:
            raise ValueError("bounding box exceeds canvas width")
        if self.y + self.height > self.canvas_height + tolerance:
            raise ValueError("bounding box exceeds canvas height")
        return self


class SourceLocation(FrozenModel):
    """Best available position of content inside the source document."""

    page_number: int | None = Field(default=None, ge=1)
    sheet_name: str | None = Field(default=None, min_length=1)
    cell_range: str | None = None
    block_index: int | None = Field(default=None, ge=0)
    asset_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    bounding_box: BoundingBox | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_location(self) -> SourceLocation:
        if self.cell_range is not None:
            if self.sheet_name is None:
                raise ValueError("cell_range requires sheet_name")
            if CELL_RANGE_PATTERN.fullmatch(self.cell_range) is None:
                raise ValueError("cell_range must use Excel A1 notation")
        if self.bounding_box is not None and self.page_number is None and self.asset_id is None:
            raise ValueError("bounding_box requires page_number or asset_id")
        return self


class Diagnostic(FrozenModel):
    """Structured non-fatal information produced during processing."""

    code: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    message: str = Field(min_length=1)
    severity: DiagnosticSeverity = DiagnosticSeverity.WARNING
    location: SourceLocation | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class SourceInfo(FrozenModel):
    """Content-derived source identity and format information."""

    name: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=SHA256_PATTERN)
    format: DocumentFormat
    media_type: str = Field(min_length=1)
    supplied_extension: str | None = Field(default=None, pattern=r"^\.[a-z0-9][a-z0-9._-]*$")
    extension_matches: bool | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("source name must be a basename")
        return value


class DocumentMetadata(FrozenModel):
    """Normalized metadata shared by all document formats."""

    title: str | None = None
    authors: tuple[str, ...] = ()
    subject: str | None = None
    keywords: tuple[str, ...] = ()
    language: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    custom: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("created_at", "modified_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("normalized timestamps must include a timezone")
        return value


class TextSpan(FrozenModel):
    """Text with inline formatting preserved for later serialization."""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    code: bool = False
    href: str | None = None


class BlockBase(FrozenModel):
    """Fields shared by every document block."""

    block_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source: SourceLocation | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class ParagraphBlock(BlockBase):
    """A paragraph represented as ordered inline spans."""

    type: Literal["paragraph"] = "paragraph"
    spans: tuple[TextSpan, ...] = ()


class HeadingBlock(BlockBase):
    """A heading with a Markdown-compatible hierarchy level."""

    type: Literal["heading"] = "heading"
    level: int = Field(ge=1, le=6)
    spans: tuple[TextSpan, ...] = Field(min_length=1)


class PageBreakBlock(BlockBase):
    """An explicit page boundary from a paginated source."""

    type: Literal["page_break"] = "page_break"


class FigureBlock(BlockBase):
    """A reference to an extracted or embedded visual asset."""

    type: Literal["figure"] = "figure"
    asset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    caption: tuple[TextSpan, ...] = ()
    alt_text: str | None = None


class ListItem(FrozenModel):
    """One list item containing arbitrary nested blocks."""

    blocks: tuple[ContentBlock, ...] = Field(min_length=1)


class ListBlock(BlockBase):
    """An ordered or unordered list with nested content."""

    type: Literal["list"] = "list"
    kind: ListKind
    start: int | None = Field(default=None, ge=1)
    items: tuple[ListItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_start(self) -> ListBlock:
        if self.kind is ListKind.UNORDERED and self.start is not None:
            raise ValueError("unordered lists cannot define start")
        return self


class TableCell(FrozenModel):
    """A positioned table cell, including spreadsheet-specific values."""

    column_index: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    is_header: bool = False
    raw_value: JSONScalar = None
    displayed_text: str = ""
    formula: str | None = None
    number_format: str | None = None
    blocks: tuple[ContentBlock, ...] = ()


class TableRow(FrozenModel):
    """A zero-based table row and its explicitly positioned cells."""

    row_index: int = Field(ge=0)
    cells: tuple[TableCell, ...] = ()


class TableBlock(BlockBase):
    """A rectangular table with merge-aware cell positioning."""

    type: Literal["table"] = "table"
    row_count: int = Field(ge=1)
    column_count: int = Field(ge=1)
    rows: tuple[TableRow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_grid(self) -> TableBlock:
        expected_rows = tuple(range(self.row_count))
        actual_rows = tuple(row.row_index for row in self.rows)
        if actual_rows != expected_rows:
            raise ValueError("table rows must be unique, ordered, and cover row_count")

        occupied: set[tuple[int, int]] = set()
        for row in self.rows:
            columns = tuple(cell.column_index for cell in row.cells)
            if columns != tuple(sorted(set(columns))):
                raise ValueError("table cells must have unique ordered column indexes")
            for cell in row.cells:
                if cell.column_index + cell.column_span > self.column_count:
                    raise ValueError("table cell exceeds column_count")
                if row.row_index + cell.row_span > self.row_count:
                    raise ValueError("table cell exceeds row_count")
                covered = {
                    (covered_row, covered_column)
                    for covered_row in range(row.row_index, row.row_index + cell.row_span)
                    for covered_column in range(
                        cell.column_index, cell.column_index + cell.column_span
                    )
                }
                if occupied.intersection(covered):
                    raise ValueError("table cells cannot overlap")
                occupied.update(covered)
        return self


class ContainerBlock(BlockBase):
    """A structural container such as a section, page, or worksheet."""

    type: Literal["container"] = "container"
    role: ContainerRole
    title: tuple[TextSpan, ...] = ()
    blocks: tuple[ContentBlock, ...] = ()


ContentBlock: TypeAlias = Annotated[
    ContainerBlock
    | FigureBlock
    | HeadingBlock
    | ListBlock
    | PageBreakBlock
    | ParagraphBlock
    | TableBlock,
    Field(discriminator="type"),
]

_content_namespace = {"ContentBlock": ContentBlock}
ContainerBlock.model_rebuild(_types_namespace=_content_namespace)
ListItem.model_rebuild(_types_namespace=_content_namespace)
TableCell.model_rebuild(_types_namespace=_content_namespace)


class AssetRef(FrozenModel):
    """Manifest entry for a binary asset kept outside the JSON IR."""

    asset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    filename: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("asset filename must be a basename")
        return value


def _walk_blocks(blocks: tuple[ContentBlock, ...]) -> tuple[ContentBlock, ...]:
    discovered: list[ContentBlock] = []
    for block in blocks:
        discovered.append(block)
        if isinstance(block, ContainerBlock):
            discovered.extend(_walk_blocks(block.blocks))
        elif isinstance(block, ListBlock):
            for item in block.items:
                discovered.extend(_walk_blocks(item.blocks))
        elif isinstance(block, TableBlock):
            for row in block.rows:
                for cell in row.cells:
                    discovered.extend(_walk_blocks(cell.blocks))
    return tuple(discovered)


class Document(FrozenModel):
    """The loss-preserving, format-independent document root."""

    schema_version: Literal["0.1"] = "0.1"
    document_id: str = Field(pattern=DOCUMENT_ID_PATTERN)
    source: SourceInfo
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
    blocks: tuple[ContentBlock, ...] = ()
    assets: tuple[AssetRef, ...] = ()
    status: DocumentStatus = DocumentStatus.COMPLETE
    diagnostics: tuple[Diagnostic, ...] = ()

    @model_validator(mode="after")
    def validate_document(self) -> Document:
        expected_id = f"sha256:{self.source.sha256}"
        if self.document_id != expected_id:
            raise ValueError("document_id must be derived from source.sha256")

        all_blocks = _walk_blocks(self.blocks)
        block_ids = tuple(block.block_id for block in all_blocks)
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block_id values must be unique across the document")

        asset_ids = tuple(asset.asset_id for asset in self.assets)
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset_id values must be unique across the document")

        known_assets = set(asset_ids)
        figure_assets = {block.asset_id for block in all_blocks if isinstance(block, FigureBlock)}
        if not figure_assets.issubset(known_assets):
            raise ValueError("every figure must reference a declared asset")
        return self
