"""Deterministic extraction quality scoring."""

from __future__ import annotations

from pydantic import Field, model_validator

from document_parser.models import (
    ContainerBlock,
    ContainerRole,
    ContentBlock,
    Diagnostic,
    DiagnosticSeverity,
    Document,
    DocumentStatus,
    FrozenModel,
    HeadingBlock,
    ListBlock,
    ParagraphBlock,
    QualityReport,
    QualityScope,
    QualityUnit,
    TableBlock,
)


class QualityOptions(FrozenModel):
    """Weights and review policy for deterministic quality assessment."""

    enabled: bool = True
    review_threshold: float = Field(default=0.75, ge=0, le=1)
    coverage_weight: float = Field(default=0.35, ge=0, le=1)
    confidence_weight: float = Field(default=0.30, ge=0, le=1)
    structure_weight: float = Field(default=0.20, ge=0, le=1)
    fidelity_weight: float = Field(default=0.15, ge=0, le=1)

    @model_validator(mode="after")
    def validate_weights(self) -> QualityOptions:
        total = (
            self.coverage_weight
            + self.confidence_weight
            + self.structure_weight
            + self.fidelity_weight
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError("quality weights must sum to 1")
        return self


_REVIEW_CODES = {
    "ocr.language_ambiguous",
    "ocr.low_confidence",
    "ocr.no_text_detected",
    "ocr.table_unstructured",
    "pdf.ocr_required",
    "quality.low_confidence",
    "reconciliation.conflict",
}
_STRUCTURE_TOKENS = ("table", "chart", "pivot", "structure", "smartart", "equation")


def _text_and_confidence(block: ContentBlock) -> tuple[int, float]:
    if block.attributes.get("active_for_rag") is False:
        return 0, 0.0
    if isinstance(block, (HeadingBlock, ParagraphBlock)):
        count = len("".join(span.text for span in block.spans).strip())
        confidence = (
            block.source.confidence
            if block.source is not None and block.source.confidence is not None
            else 1.0
        )
        return count, confidence * count
    if isinstance(block, ContainerBlock):
        return _blocks_text_and_confidence(block.blocks)
    if isinstance(block, ListBlock):
        return _blocks_text_and_confidence(
            tuple(child for item in block.items for child in item.blocks)
        )
    if isinstance(block, TableBlock):
        count = 0
        weighted = 0.0
        table_confidence = (
            block.source.confidence
            if block.source is not None and block.source.confidence is not None
            else 1.0
        )
        for row in block.rows:
            for cell in row.cells:
                cell_count = len(cell.displayed_text.strip())
                count += cell_count
                weighted += table_confidence * cell_count
                nested_count, nested_weighted = _blocks_text_and_confidence(cell.blocks)
                if cell_count == 0:
                    count += nested_count
                    weighted += nested_weighted
        return count, weighted
    return 0, 0.0


def _blocks_text_and_confidence(blocks: tuple[ContentBlock, ...]) -> tuple[int, float]:
    values = tuple(_text_and_confidence(block) for block in blocks)
    return sum(item[0] for item in values), sum(item[1] for item in values)


def _meaningful_unit(block: ContainerBlock) -> bool:
    return bool(block.blocks) or block.attributes.get("scan_candidate") is True


def _quality_units(document: Document) -> tuple[QualityUnit, ...]:
    containers = tuple(
        block
        for block in document.blocks
        if isinstance(block, ContainerBlock)
        and block.role in {ContainerRole.PAGE, ContainerRole.SHEET}
        and _meaningful_unit(block)
    )
    if not containers:
        count, weighted = _blocks_text_and_confidence(document.blocks)
        confidence = weighted / count if count else 1.0
        return (
            QualityUnit(
                scope=QualityScope.DOCUMENT,
                identifier=document.source.name,
                text_characters=count,
                confidence=round(confidence, 4),
                score=round(confidence if count else 1.0, 4),
                flags=() if count else ("quality.no_text",),
            ),
        )
    units: list[QualityUnit] = []
    for index, container in enumerate(containers, start=1):
        count, weighted = _blocks_text_and_confidence(container.blocks)
        confidence = weighted / count if count else 0.0
        if container.role is ContainerRole.PAGE:
            identifier = str(
                container.source.page_number
                if container.source and container.source.page_number
                else index
            )
            scope = QualityScope.PAGE
        else:
            identifier = str(container.attributes.get("sheet_name", f"Sheet {index}"))
            scope = QualityScope.SHEET
        flags = () if count else ("quality.no_text",)
        units.append(
            QualityUnit(
                scope=scope,
                identifier=identifier,
                text_characters=count,
                confidence=round(confidence, 4),
                score=round(confidence if count else 0.0, 4),
                flags=flags,
            )
        )
    return tuple(units)


def _structure_score(document: Document) -> float:
    damaging = sum(
        1
        for diagnostic in document.diagnostics
        if any(token in diagnostic.code for token in _STRUCTURE_TOKENS)
        and diagnostic.severity is not DiagnosticSeverity.INFO
    )
    return max(0.0, 1.0 - 0.1 * damaging)


def _fidelity_score(diagnostics: tuple[Diagnostic, ...]) -> float:
    penalty = sum(
        0.25
        if diagnostic.severity is DiagnosticSeverity.ERROR
        else 0.05
        if diagnostic.severity is DiagnosticSeverity.WARNING
        else 0.0
        for diagnostic in diagnostics
    )
    return max(0.0, 1.0 - penalty)


def assess_quality(document: Document, *, options: QualityOptions | None = None) -> QualityReport:
    """Calculate a transparent, deterministic quality report."""

    resolved = options or QualityOptions()
    units = _quality_units(document)
    coverage = sum(unit.text_characters > 0 for unit in units) / len(units)
    total_characters = sum(unit.text_characters for unit in units)
    confidence = (
        sum(unit.confidence * unit.text_characters for unit in units) / total_characters
        if total_characters
        else 1.0
        if all(unit.scope is QualityScope.DOCUMENT for unit in units)
        else 0.0
    )
    structure = _structure_score(document)
    fidelity = _fidelity_score(document.diagnostics)
    overall = (
        resolved.coverage_weight * coverage
        + resolved.confidence_weight * confidence
        + resolved.structure_weight * structure
        + resolved.fidelity_weight * fidelity
    )
    flags = {
        diagnostic.code
        for diagnostic in document.diagnostics
        if diagnostic.severity is not DiagnosticSeverity.INFO
    }
    if coverage < 1:
        flags.add("quality.incomplete_coverage")
    if confidence < resolved.review_threshold:
        flags.add("quality.low_confidence")
    return QualityReport(
        overall_score=round(overall, 4),
        coverage_score=round(coverage, 4),
        confidence_score=round(confidence, 4),
        structure_score=round(structure, 4),
        fidelity_score=round(fidelity, 4),
        units=units,
        flags=tuple(sorted(flags)),
    )


def apply_quality(document: Document, options: QualityOptions) -> Document:
    """Attach quality and resolve status without hiding fatal partial outcomes."""

    if not options.enabled:
        return document
    report = assess_quality(document, options=options)
    has_review_signal = any(code in _REVIEW_CODES for code in report.flags)
    if document.status is DocumentStatus.PARTIAL:
        status = DocumentStatus.PARTIAL
    elif (
        document.status is DocumentStatus.NEEDS_REVIEW
        or report.overall_score < options.review_threshold
        or has_review_signal
    ):
        status = DocumentStatus.NEEDS_REVIEW
    else:
        status = DocumentStatus.COMPLETE
    payload = document.model_dump(mode="python")
    payload.update({"schema_version": "0.2", "quality": report, "status": status})
    return Document.model_validate(payload)
