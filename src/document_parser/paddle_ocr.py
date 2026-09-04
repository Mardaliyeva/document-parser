"""Lazy PaddleOCR implementation of the engine-neutral OCR contract."""

from __future__ import annotations

import io
import json
import os
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from threading import RLock
from typing import Any, cast

from document_parser.exceptions import (
    OcrConfigurationError,
    OcrDependencyNotAvailableError,
    OcrExecutionError,
    OcrModelNotAvailableError,
)
from document_parser.models import BoundingBox, CoordinateUnit
from document_parser.ocr import (
    OcrEngineDiagnostic,
    OcrOptions,
    OcrPageInput,
    OcrPageResult,
    OcrProfile,
    OcrRegion,
    OcrRegionKind,
    OcrTable,
    OcrTableCell,
    OcrTextLine,
)
from document_parser.ocr_models import required_model_names, resolve_model_store, verify_ocr_models

_MODEL_SOURCE_ENV = "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"
_MODEL_SOURCE_LOCK = RLock()


@contextmanager
def _offline_model_sources() -> Iterator[None]:
    with _MODEL_SOURCE_LOCK:
        previous = os.environ.get(_MODEL_SOURCE_ENV)
        os.environ[_MODEL_SOURCE_ENV] = "True"
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(_MODEL_SOURCE_ENV, None)
            else:
                os.environ[_MODEL_SOURCE_ENV] = previous


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    confidence: float
    box: BoundingBox
    language: str | None = None


def _mapping(value: object) -> Mapping[str, Any]:
    raw = getattr(value, "json", value)
    if callable(raw):
        raw = raw()
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, Mapping):
        raise OcrExecutionError("PaddleOCR returned an unsupported result payload")
    nested = raw.get("res")
    return cast(Mapping[str, Any], nested) if isinstance(nested, Mapping) else raw


def _sequence(value: object) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    tolist = getattr(value, "tolist", None)
    converted = tolist() if callable(tolist) else ()
    return converted if isinstance(converted, Sequence) else ()


def _box(value: object, width: int, height: int) -> BoundingBox | None:
    coordinates = _sequence(value)
    if len(coordinates) == 4 and all(isinstance(item, (int, float)) for item in coordinates):
        x0, y0, x1, y1 = (float(item) for item in coordinates)
    else:
        points = tuple(_sequence(item) for item in coordinates)
        usable = tuple(
            (float(point[0]), float(point[1]))
            for point in points
            if len(point) >= 2
            and isinstance(point[0], (int, float))
            and isinstance(point[1], (int, float))
        )
        if not usable:
            return None
        x0 = min(point[0] for point in usable)
        y0 = min(point[1] for point in usable)
        x1 = max(point[0] for point in usable)
        y1 = max(point[1] for point in usable)
    safe_x0 = min(max(0.0, x0), width - 1e-6)
    safe_y0 = min(max(0.0, y0), height - 1e-6)
    safe_x1 = min(max(safe_x0 + 1e-6, x1), float(width))
    safe_y1 = min(max(safe_y0 + 1e-6, y1), float(height))
    return BoundingBox(
        x=safe_x0,
        y=safe_y0,
        width=safe_x1 - safe_x0,
        height=safe_y1 - safe_y0,
        canvas_width=width,
        canvas_height=height,
        unit=CoordinateUnit.PIXEL,
    )


def _first_prediction(pipeline: object, image: object) -> Mapping[str, Any]:
    predict = getattr(pipeline, "predict", None)
    if not callable(predict):
        raise OcrExecutionError("PaddleOCR pipeline has no predict method")
    predictions = tuple(predict(image))
    if not predictions:
        return {}
    return _mapping(predictions[0])


def _tokens(payload: Mapping[str, Any], width: int, height: int) -> tuple[_Token, ...]:
    texts = _sequence(payload.get("rec_texts", ()))
    scores = _sequence(payload.get("rec_scores", ()))
    polygons = _sequence(payload.get("rec_polys", payload.get("dt_polys", ())))
    boxes = _sequence(payload.get("rec_boxes", ()))
    result: list[_Token] = []
    for index, raw_text in enumerate(texts):
        text = str(raw_text).strip()
        if not text:
            continue
        raw_box = (
            polygons[index] if index < len(polygons) else boxes[index] if index < len(boxes) else ()
        )
        bounding_box = _box(raw_box, width, height)
        if bounding_box is None:
            continue
        raw_score = scores[index] if index < len(scores) else 0.0
        confidence = min(1.0, max(0.0, float(raw_score)))
        result.append(_Token(text=text, confidence=confidence, box=bounding_box))
    return tuple(sorted(result, key=lambda token: (round(token.box.y, 3), token.box.x)))


def _script_counts(value: str) -> tuple[int, int]:
    latin = 0
    cyrillic = 0
    for character in value:
        name = unicodedata.name(character, "")
        latin += "LATIN" in name
        cyrillic += "CYRILLIC" in name
    return latin, cyrillic


def _choose_recognition(
    latin: _Token, russian_text: str, russian_score: float
) -> tuple[_Token, bool]:
    if not russian_text:
        return latin, False
    latin_count, latin_cyrillic = _script_counts(latin.text)
    russian_latin, russian_cyrillic = _script_counts(russian_text)
    score_gap = abs(latin.confidence - russian_score)
    ambiguous = latin.text != russian_text and score_gap <= 0.03
    use_russian = False
    if russian_cyrillic > russian_latin and russian_cyrillic >= latin_cyrillic:
        use_russian = russian_score >= latin.confidence - 0.03
    elif latin_count == 0 and latin_cyrillic == 0:
        use_russian = russian_score > latin.confidence
    if not use_russian:
        return _Token(latin.text, latin.confidence, latin.box, "az-en"), ambiguous
    return _Token(russian_text, russian_score, latin.box, "ru"), ambiguous


def _inside(token: _Token, box: BoundingBox) -> bool:
    center_x = token.box.x + token.box.width / 2
    center_y = token.box.y + token.box.height / 2
    return box.x <= center_x <= box.x + box.width and box.y <= center_y <= box.y + box.height


def _line(token: _Token) -> OcrTextLine:
    return OcrTextLine(
        text=token.text,
        bounding_box=token.box,
        confidence=token.confidence,
        language=token.language,
    )


def _region_confidence(tokens: Iterable[_Token], fallback: float = 0.0) -> float:
    values = tuple(tokens)
    characters = sum(max(1, len(token.text)) for token in values)
    if not characters:
        return fallback
    return sum(token.confidence * max(1, len(token.text)) for token in values) / characters


def _looks_like_list(value: str) -> bool:
    marker, separator, _text = value.lstrip().partition(" ")
    if not separator:
        return False
    return marker in {"-", "*", "•"} or (marker.endswith((".", ")")) and marker[:-1].isdigit())


def _kind(label: str) -> OcrRegionKind:
    normalized = label.strip().lower()
    if normalized in {"doc_title", "document_title", "title"}:
        return OcrRegionKind.DOCUMENT_TITLE
    if normalized in {"paragraph_title", "section_title", "heading"}:
        return OcrRegionKind.PARAGRAPH_TITLE
    if normalized in {"table"}:
        return OcrRegionKind.TABLE
    if normalized in {"image", "figure", "chart"}:
        return OcrRegionKind.FIGURE
    if normalized in {"figure_title", "table_title", "caption"}:
        return OcrRegionKind.CAPTION
    if normalized in {"list", "list_item"}:
        return OcrRegionKind.LIST
    return OcrRegionKind.TEXT


def _derive_table(
    table_payload: Mapping[str, Any],
    tokens: tuple[_Token, ...],
    width: int,
    height: int,
) -> OcrTable | None:
    raw_boxes = _sequence(table_payload.get("cell_box_list", table_payload.get("cell_boxes", ())))
    boxes = tuple(box for raw in raw_boxes if (box := _box(raw, width, height)) is not None)
    if not boxes:
        return None
    x_edges = sorted({round(value, 2) for box in boxes for value in (box.x, box.x + box.width)})
    y_edges = sorted({round(value, 2) for box in boxes for value in (box.y, box.y + box.height)})
    if len(x_edges) < 2 or len(y_edges) < 2:
        return None
    cells: list[OcrTableCell] = []
    occupied: set[tuple[int, int]] = set()
    for box in sorted(boxes, key=lambda item: (item.y, item.x)):
        column = x_edges.index(round(box.x, 2))
        row = y_edges.index(round(box.y, 2))
        column_span = x_edges.index(round(box.x + box.width, 2)) - column
        row_span = y_edges.index(round(box.y + box.height, 2)) - row
        covered = {
            (covered_row, covered_column)
            for covered_row in range(row, row + max(1, row_span))
            for covered_column in range(column, column + max(1, column_span))
        }
        if occupied.intersection(covered):
            return None
        occupied.update(covered)
        cell_tokens = tuple(token for token in tokens if _inside(token, box))
        cells.append(
            OcrTableCell(
                row_index=row,
                column_index=column,
                row_span=max(1, row_span),
                column_span=max(1, column_span),
                text=" ".join(token.text for token in cell_tokens),
                bounding_box=box,
                confidence=_region_confidence(cell_tokens) if cell_tokens else None,
            )
        )
    return OcrTable(
        row_count=len(y_edges) - 1,
        column_count=len(x_edges) - 1,
        cells=tuple(cells),
    )


class PaddleOcrEngine:
    """Local PaddleOCR backend with explicit local model directories."""

    name = "paddleocr"

    def __init__(self, options: OcrOptions, *, source_name: str | None = None) -> None:
        unsupported = set(options.languages).difference({"az", "en", "ru"})
        if unsupported:
            raise OcrConfigurationError(
                f"built-in PaddleOCR engine does not support configured languages: {sorted(unsupported)}",
                source_name=source_name,
            )
        self._options = options
        self._store = resolve_model_store(options.model_store)
        self._models = required_model_names((options.profile,), options.languages)
        report = verify_ocr_models(self._store, required=self._models)
        if not report.valid:
            detail = ", ".join((*report.missing, *report.corrupted))
            raise OcrModelNotAvailableError(
                f"OCR model store is missing or invalid: {detail}", source_name=source_name
            )
        try:
            with _offline_model_sources():
                import_module("paddle")
                paddleocr = import_module("paddleocr")
                self._pipeline = self._build_pipeline(paddleocr)
                self._russian = (
                    self._build_russian(paddleocr) if "ru" in options.languages else None
                )
        except ImportError as exc:
            raise OcrDependencyNotAvailableError(
                "PaddleOCR requires paddleocr and a PaddlePaddle inference runtime",
                source_name=source_name,
            ) from exc

    def _path(self, name: str) -> str:
        return str(self._store / name)

    def _device(self) -> str:
        return "gpu:0" if self._options.device.value == "gpu" else "cpu"

    def _common(self, *, size: str) -> dict[str, object]:
        return {
            "device": self._device(),
            "use_doc_orientation_classify": self._options.use_orientation,
            "doc_orientation_classify_model_dir": self._path("PP-LCNet_x1_0_doc_ori"),
            "use_doc_unwarping": (
                self._options.use_unwarping and self._options.profile is OcrProfile.STRUCTURED
            ),
            "use_textline_orientation": True,
            "textline_orientation_model_dir": self._path("PP-LCNet_x1_0_textline_ori"),
            "text_detection_model_name": f"PP-OCRv6_{size}_det",
            "text_detection_model_dir": self._path(f"PP-OCRv6_{size}_det"),
            "text_recognition_model_name": f"PP-OCRv6_{size}_rec",
            "text_recognition_model_dir": self._path(f"PP-OCRv6_{size}_rec"),
        }

    def _build_pipeline(self, module: object) -> object:
        try:
            if self._options.profile is OcrProfile.TEXT:
                factory = cast(Any, module).PaddleOCR
                return factory(lang="az", ocr_version="PP-OCRv6", **self._common(size="small"))
            factory = cast(Any, module).PPStructureV3
            arguments = {
                **self._common(size="medium"),
                "doc_unwarping_model_dir": self._path("UVDoc"),
                "layout_detection_model_dir": self._path("PP-DocLayoutV3"),
                "use_table_recognition": True,
                "use_formula_recognition": False,
                "use_chart_recognition": False,
                "use_seal_recognition": False,
                "table_classification_model_dir": self._path("PP-LCNet_x1_0_table_cls"),
                "wired_table_structure_recognition_model_dir": self._path("SLANeXt_wired"),
                "wireless_table_structure_recognition_model_dir": self._path("SLANeXt_wireless"),
                "wired_table_cells_detection_model_dir": self._path(
                    "RT-DETR-L_wired_table_cell_det"
                ),
                "wireless_table_cells_detection_model_dir": self._path(
                    "RT-DETR-L_wireless_table_cell_det"
                ),
            }
            return factory(lang="az", **arguments)
        except (AttributeError, TypeError, ValueError) as exc:
            raise OcrConfigurationError(
                "installed PaddleOCR version cannot create the configured local pipeline"
            ) from exc

    def _build_russian(self, module: object) -> object:
        try:
            factory = cast(Any, module).TextRecognition
            return factory(
                model_name="eslav_PP-OCRv5_mobile_rec",
                model_dir=self._path("eslav_PP-OCRv5_mobile_rec"),
                device=self._device(),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise OcrConfigurationError(
                "installed PaddleOCR version cannot create the Russian recognizer"
            ) from exc

    def _russian_result(self, image: object, token: _Token) -> tuple[str, float]:
        if self._russian is None:
            return "", 0.0
        from PIL import Image

        pil = cast(Image.Image, image)
        crop = pil.crop(
            (
                int(token.box.x),
                int(token.box.y),
                max(int(token.box.x + token.box.width), int(token.box.x) + 1),
                max(int(token.box.y + token.box.height), int(token.box.y) + 1),
            )
        )
        try:
            numpy = import_module("numpy")
            payload = _first_prediction(self._russian, numpy.asarray(crop))
        except Exception as exc:
            raise OcrExecutionError("Russian OCR recognition failed") from exc
        text = str(payload.get("rec_text", "")).strip()
        score = min(1.0, max(0.0, float(payload.get("rec_score", 0.0))))
        return text, score

    def _dual_language_tokens(
        self, payload: Mapping[str, Any], image: object, width: int, height: int
    ) -> tuple[tuple[_Token, ...], bool]:
        recognized: list[_Token] = []
        ambiguous = False
        for token in _tokens(payload, width, height):
            russian_text, russian_score = self._russian_result(image, token)
            selected, uncertain = _choose_recognition(token, russian_text, russian_score)
            recognized.append(selected)
            ambiguous = ambiguous or uncertain
        return tuple(recognized), ambiguous

    def _text_regions(
        self, tokens: tuple[_Token, ...], width: int, height: int
    ) -> tuple[OcrRegion, ...]:
        return tuple(
            OcrRegion(
                order=index,
                kind=OcrRegionKind.LIST if _looks_like_list(token.text) else OcrRegionKind.TEXT,
                bounding_box=token.box,
                confidence=token.confidence,
                lines=(_line(token),),
            )
            for index, token in enumerate(tokens)
        )

    def _structured_regions(
        self,
        payload: Mapping[str, Any],
        tokens: tuple[_Token, ...],
        width: int,
        height: int,
    ) -> tuple[tuple[OcrRegion, ...], tuple[OcrEngineDiagnostic, ...]]:
        layout_payload = payload.get("layout_det_res", {})
        layout = (
            cast(Mapping[str, Any], layout_payload) if isinstance(layout_payload, Mapping) else {}
        )
        boxes = _sequence(layout.get("boxes", ()))
        table_payloads = tuple(
            _mapping(item) for item in _sequence(payload.get("table_res_list", ()))
        )
        regions: list[OcrRegion] = []
        diagnostics: list[OcrEngineDiagnostic] = []
        for raw in boxes:
            if not isinstance(raw, Mapping):
                continue
            bounding_box = _box(raw.get("coordinate", raw.get("bbox", ())), width, height)
            if bounding_box is None:
                continue
            kind = _kind(str(raw.get("label", "text")))
            contained = tuple(token for token in tokens if _inside(token, bounding_box))
            score = min(1.0, max(0.0, float(raw.get("score", 0.0))))
            if kind is OcrRegionKind.TABLE:
                matching = next(
                    (
                        candidate
                        for candidate in table_payloads
                        if (
                            candidate_box := _box(
                                candidate.get("bbox", candidate.get("table_region_id", ())),
                                width,
                                height,
                            )
                        )
                        is not None
                        and _inside(
                            _Token("table", 1.0, candidate_box),
                            bounding_box,
                        )
                    ),
                    table_payloads[0] if table_payloads else None,
                )
                table = (
                    _derive_table(matching, contained, width, height)
                    if matching is not None
                    else None
                )
                if table is None:
                    diagnostics.append(
                        OcrEngineDiagnostic(
                            code="ocr.table_unstructured",
                            message="A detected table could not be converted into a reliable grid.",
                        )
                    )
                    kind = OcrRegionKind.TEXT
                else:
                    regions.append(
                        OcrRegion(
                            order=len(regions),
                            kind=OcrRegionKind.TABLE,
                            bounding_box=bounding_box,
                            confidence=_region_confidence(contained, score),
                            table=table,
                        )
                    )
                    continue
            if kind is OcrRegionKind.FIGURE:
                regions.append(
                    OcrRegion(
                        order=len(regions),
                        kind=kind,
                        bounding_box=bounding_box,
                        confidence=score,
                    )
                )
            elif contained:
                regions.append(
                    OcrRegion(
                        order=len(regions),
                        kind=kind,
                        bounding_box=bounding_box,
                        confidence=_region_confidence(contained, score),
                        lines=tuple(_line(token) for token in contained),
                    )
                )
        if not regions:
            return self._text_regions(tokens, width, height), tuple(diagnostics)
        return tuple(regions), tuple(diagnostics)

    def recognize(self, page: OcrPageInput, options: OcrOptions) -> OcrPageResult:
        """Run the configured local Paddle pipeline for one PNG page."""

        if options != self._options:
            raise OcrConfigurationError("OCR engine options differ from parser options")
        from PIL import Image

        try:
            with _offline_model_sources():
                image = Image.open(io.BytesIO(page.image_png)).convert("RGB")
                numpy = import_module("numpy")
                payload = _first_prediction(self._pipeline, numpy.asarray(image))
                overall = payload.get("overall_ocr_res", payload)
                token_payload = (
                    cast(Mapping[str, Any], overall) if isinstance(overall, Mapping) else payload
                )
                tokens, ambiguous = self._dual_language_tokens(
                    token_payload, image, page.width_pixels, page.height_pixels
                )
                if options.profile is OcrProfile.STRUCTURED:
                    regions, diagnostics = self._structured_regions(
                        payload, tokens, page.width_pixels, page.height_pixels
                    )
                else:
                    regions = self._text_regions(tokens, page.width_pixels, page.height_pixels)
                    diagnostics = ()
        except (OcrConfigurationError, OcrExecutionError):
            raise
        except Exception as exc:
            raise OcrExecutionError("PaddleOCR page inference failed") from exc
        return OcrPageResult(
            page_number=page.page_number,
            regions=regions,
            engine=self.name,
            models=self._models,
            diagnostics=diagnostics,
            ambiguous_language=ambiguous,
        )
