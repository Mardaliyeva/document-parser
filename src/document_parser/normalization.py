"""Fact-preserving normalization and native/OCR reconciliation."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from pydantic import Field, JsonValue

from document_parser.models import (
    BoundingBox,
    ContainerBlock,
    ContentBlock,
    Diagnostic,
    DiagnosticSeverity,
    Document,
    FigureBlock,
    FrozenModel,
    HeadingBlock,
    ListBlock,
    ListItem,
    ParagraphBlock,
    SourceLocation,
    TableBlock,
    TableCell,
    TableRow,
    TextSpan,
)


class NormalizationOptions(FrozenModel):
    """Conservative cleanup and source-reconciliation thresholds."""

    enabled: bool = True
    reconcile_ocr: bool = True
    normalize_unicode: bool = True
    normalize_whitespace: bool = True
    repair_heading_levels: bool = True
    hide_repeated_margins: bool = True
    duplicate_text_similarity: float = Field(default=0.96, ge=0, le=1)
    geometry_overlap: float = Field(default=0.50, ge=0, le=1)
    ocr_confidence_margin: float = Field(default=0.05, ge=0, le=1)


_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")


def _plain_text(block: ContentBlock) -> str:
    if isinstance(block, (HeadingBlock, ParagraphBlock)):
        return "".join(span.text for span in block.spans)
    if isinstance(block, ContainerBlock):
        return " ".join(filter(None, (_plain_text(child) for child in block.blocks)))
    if isinstance(block, ListBlock):
        return " ".join(
            filter(None, (_plain_text(child) for item in block.items for child in item.blocks))
        )
    if isinstance(block, TableBlock):
        return " ".join(
            cell.displayed_text or _plain_text_from_blocks(cell.blocks)
            for row in block.rows
            for cell in row.cells
        )
    if isinstance(block, FigureBlock):
        return block.alt_text or "".join(span.text for span in block.caption)
    return ""


def _plain_text_from_blocks(blocks: tuple[ContentBlock, ...]) -> str:
    return " ".join(filter(None, (_plain_text(block) for block in blocks)))


def _comparison_text(block: ContentBlock) -> str:
    return " ".join(unicodedata.normalize("NFC", _plain_text(block)).casefold().split())


def _overlap(first: BoundingBox | None, second: BoundingBox | None) -> float:
    if first is None or second is None or first.unit is not second.unit:
        return 0.0
    left = max(first.x, second.x)
    top = max(first.y, second.y)
    right = min(first.x + first.width, second.x + second.width)
    bottom = min(first.y + first.height, second.y + second.height)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    minimum = min(first.width * first.height, second.width * second.height)
    return intersection / minimum if minimum else 0.0


def _box(block: ContentBlock) -> BoundingBox | None:
    return block.source.bounding_box if block.source is not None else None


def _confidence(block: ContentBlock, *, scan_candidate: bool) -> float:
    if block.source is not None and block.source.confidence is not None:
        return block.source.confidence
    return 0.5 if scan_candidate else 1.0


def _set_active(block: ContentBlock, active: bool, details: dict[str, JsonValue]) -> ContentBlock:
    if isinstance(block, ContainerBlock):
        block = block.model_copy(
            update={"blocks": tuple(_set_active(child, active, details) for child in block.blocks)}
        )
    elif isinstance(block, ListBlock):
        block = block.model_copy(
            update={
                "items": tuple(
                    item.model_copy(
                        update={
                            "blocks": tuple(
                                _set_active(child, active, details) for child in item.blocks
                            )
                        }
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
                                            _set_active(child, active, details)
                                            for child in cell.blocks
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
    return block.model_copy(
        update={"attributes": {**block.attributes, "active_for_rag": active, **details}}
    )


def _decision_details(
    reason: str,
    peer: ContentBlock,
    similarity: float,
    overlap: float,
    confidence: float,
) -> dict[str, JsonValue]:
    return {
        "reconciliation_decision": reason,
        "reconciliation_peer": peer.block_id,
        "reconciliation_text_similarity": round(similarity, 6),
        "reconciliation_geometry_overlap": round(overlap, 6),
        "reconciliation_confidence": round(confidence, 6),
    }


def _reconcile_page(
    page: ContainerBlock, options: NormalizationOptions
) -> tuple[ContainerBlock, tuple[Diagnostic, ...]]:
    if page.attributes.get("ocr_applied") is not True:
        return page, ()
    scan_candidate = page.attributes.get("scan_candidate") is True
    blocks = list(page.blocks)
    ocr_indexes = [
        index
        for index, block in enumerate(blocks)
        if block.attributes.get("extraction_method") == "ocr"
    ]
    native_indexes = [
        index
        for index, block in enumerate(blocks)
        if block.attributes.get("extraction_method") == "native"
        and not isinstance(block, FigureBlock)
    ]
    for index in native_indexes:
        native = blocks[index]
        blocks[index] = _set_active(
            native,
            True,
            {"reconciliation_decision": "complement"},
        )

    unmatched = set(native_indexes)
    diagnostics: list[Diagnostic] = []
    for ocr_index in ocr_indexes:
        ocr = blocks[ocr_index]
        ocr_text = _comparison_text(ocr)
        candidates = []
        for native_index in sorted(unmatched):
            native = blocks[native_index]
            geometry = _overlap(_box(ocr), _box(native))
            if geometry < options.geometry_overlap:
                continue
            native_text = _comparison_text(native)
            similarity = (
                SequenceMatcher(None, ocr_text, native_text, autojunk=False).ratio()
                if ocr_text and native_text
                else 0.0
            )
            candidates.append((geometry, similarity, native_index))
        if not candidates:
            blocks[ocr_index] = _set_active(ocr, True, {"reconciliation_decision": "complement"})
            continue
        geometry, similarity, native_index = max(
            candidates, key=lambda item: (item[0], item[1], -item[2])
        )
        unmatched.remove(native_index)
        native = blocks[native_index]
        ocr_confidence = _confidence(ocr, scan_candidate=scan_candidate)
        native_confidence = _confidence(native, scan_candidate=scan_candidate)
        prefer_ocr = scan_candidate and (
            ocr_confidence >= native_confidence + options.ocr_confidence_margin
        )
        duplicate = similarity >= options.duplicate_text_similarity
        reason = "duplicate" if duplicate else "conflict"
        blocks[ocr_index] = _set_active(
            ocr,
            prefer_ocr,
            _decision_details(reason, native, similarity, geometry, ocr_confidence),
        )
        blocks[native_index] = _set_active(
            native,
            not prefer_ocr,
            _decision_details(reason, ocr, similarity, geometry, native_confidence),
        )
        if not duplicate:
            diagnostics.append(
                Diagnostic(
                    code="reconciliation.conflict",
                    message="Overlapping native and OCR text disagree; one source was selected.",
                    severity=DiagnosticSeverity.WARNING,
                    location=SourceLocation(page_number=_page_number(page)),
                    details={
                        "native_block_id": native.block_id,
                        "ocr_block_id": ocr.block_id,
                        "selected_source": "ocr" if prefer_ocr else "native",
                        "text_similarity": round(similarity, 6),
                        "geometry_overlap": round(geometry, 6),
                    },
                )
            )
    return page.model_copy(update={"blocks": tuple(blocks)}), tuple(diagnostics)


def _page_number(page: ContainerBlock) -> int:
    return page.source.page_number if page.source and page.source.page_number else 1


def _reconcile_document(document: Document, options: NormalizationOptions) -> Document:
    diagnostics = [
        diagnostic
        for diagnostic in document.diagnostics
        if diagnostic.code != "reconciliation.conflict"
    ]
    blocks: list[ContentBlock] = []
    for block in document.blocks:
        if isinstance(block, ContainerBlock):
            block, page_diagnostics = _reconcile_page(block, options)
            diagnostics.extend(page_diagnostics)
        blocks.append(block)
    return document.model_copy(update={"blocks": tuple(blocks), "diagnostics": tuple(diagnostics)})


def _normalize_text(value: str, options: NormalizationOptions) -> str:
    normalized = unicodedata.normalize("NFC", value) if options.normalize_unicode else value
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "".join(
        character
        for character in normalized
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    )
    if options.normalize_whitespace:
        normalized = "\n".join(
            _HORIZONTAL_SPACE.sub(" ", line).strip() for line in normalized.split("\n")
        )
    return normalized


def _span_signature(span: TextSpan) -> tuple[bool, bool, bool, bool, bool, str | None]:
    return (
        span.bold,
        span.italic,
        span.underline,
        span.strikethrough,
        span.code,
        span.href,
    )


def _normalize_spans(
    spans: tuple[TextSpan, ...], options: NormalizationOptions
) -> tuple[tuple[TextSpan, ...], bool]:
    normalized: list[TextSpan] = []
    changed = False
    for span in spans:
        text = _normalize_text(span.text, options)
        changed = changed or text != span.text
        candidate = span.model_copy(update={"text": text})
        if normalized and _span_signature(normalized[-1]) == _span_signature(candidate):
            previous = normalized.pop()
            normalized.append(previous.model_copy(update={"text": previous.text + candidate.text}))
            changed = True
        else:
            normalized.append(candidate)
    return tuple(normalized), changed


def _normalize_block(block: ContentBlock, options: NormalizationOptions) -> ContentBlock:
    if isinstance(block, ContainerBlock):
        title, changed = _normalize_spans(block.title, options)
        children = _normalize_sequence(block.blocks, options)
        attributes = dict(block.attributes)
        if changed:
            attributes["normalization_original_title"] = [
                span.model_dump(mode="json") for span in block.title
            ]
        return block.model_copy(
            update={"title": title, "blocks": children, "attributes": attributes}
        )
    if isinstance(block, ListBlock):
        return block.model_copy(
            update={
                "items": tuple(
                    ListItem(blocks=_normalize_sequence(item.blocks, options))
                    for item in block.items
                )
            }
        )
    if isinstance(block, TableBlock):
        rows = tuple(
            TableRow(
                row_index=row.row_index,
                cells=tuple(
                    TableCell(
                        **{
                            **cell.model_dump(exclude={"blocks"}),
                            "blocks": _normalize_sequence(cell.blocks, options),
                        }
                    )
                    for cell in row.cells
                ),
            )
            for row in block.rows
        )
        return block.model_copy(update={"rows": rows})
    if isinstance(block, (HeadingBlock, ParagraphBlock)):
        spans, changed = _normalize_spans(block.spans, options)
        attributes = dict(block.attributes)
        if changed:
            attributes["normalization_original_spans"] = [
                span.model_dump(mode="json") for span in block.spans
            ]
        if isinstance(block, ParagraphBlock) and not "".join(span.text for span in spans).strip():
            attributes.update(
                {"active_for_rag": False, "normalization_decision": "empty_or_decorative"}
            )
        return block.model_copy(update={"spans": spans, "attributes": attributes})
    return block


def _normalize_sequence(
    blocks: tuple[ContentBlock, ...], options: NormalizationOptions
) -> tuple[ContentBlock, ...]:
    normalized: list[ContentBlock] = []
    previous_heading: int | None = None
    for raw in blocks:
        block = _normalize_block(raw, options)
        if isinstance(block, HeadingBlock):
            if (
                options.repair_heading_levels
                and previous_heading is not None
                and block.level > previous_heading + 1
            ):
                attributes = {
                    **block.attributes,
                    "normalization_original_heading_level": block.level,
                }
                block = block.model_copy(
                    update={"level": previous_heading + 1, "attributes": attributes}
                )
            previous_heading = block.level
        if options.hide_repeated_margins and block.attributes.get("repeated_margin") is True:
            block = _set_active(block, False, {"normalization_decision": "repeated_margin"})
        normalized.append(block)
    return tuple(normalized)


def normalize_document(
    document: Document, *, options: NormalizationOptions | None = None
) -> Document:
    """Reconcile extraction sources and conservatively normalize a Document."""

    resolved = options or NormalizationOptions()
    if not resolved.enabled:
        return document
    reconciled = _reconcile_document(document, resolved) if resolved.reconcile_ocr else document
    blocks = _normalize_sequence(reconciled.blocks, resolved)
    return reconciled.model_copy(update={"blocks": blocks})
