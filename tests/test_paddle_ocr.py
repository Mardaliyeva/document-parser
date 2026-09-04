"""Tests for the lazy PaddleOCR bridge and language reconciliation helpers."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from document_parser import (
    BoundingBox,
    CoordinateUnit,
    OcrConfigurationError,
    OcrDependencyNotAvailableError,
    OcrMode,
    OcrModelNotAvailableError,
    OcrOptions,
    OcrPageInput,
    OcrProfile,
    OcrRegionKind,
)
from document_parser.exceptions import OcrExecutionError
from document_parser.ocr_models import OcrModelReport
from document_parser.paddle_ocr import (
    PaddleOcrEngine,
    _box,
    _choose_recognition,
    _derive_table,
    _kind,
    _looks_like_list,
    _mapping,
    _region_confidence,
    _sequence,
    _Token,
    _tokens,
)


def pixel_box(x: float = 0, y: float = 0, width: float = 20, height: float = 10) -> BoundingBox:
    return BoundingBox(
        x=x,
        y=y,
        width=width,
        height=height,
        canvas_width=100,
        canvas_height=100,
        unit=CoordinateUnit.PIXEL,
    )


def page_input() -> OcrPageInput:
    stream = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(stream, format="PNG")
    return OcrPageInput(
        page_number=1,
        image_png=stream.getvalue(),
        width_pixels=100,
        height_pixels=100,
        source_width_points=50,
        source_height_points=50,
        dpi=300,
    )


class JsonResult:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def json(self) -> str:
        return json.dumps({"res": self.payload})


class ArrayLike:
    def __init__(self, value: list[object]) -> None:
        self.value = value

    def tolist(self) -> list[object]:
        return self.value


class FakePipeline:
    def __init__(self, payload: dict[str, object], *, empty: bool = False) -> None:
        self.payload = payload
        self.empty = empty

    def predict(self, _image: object) -> list[object]:
        return [] if self.empty else [JsonResult(self.payload)]


class FakeRussian:
    def __init__(self, text: str = "Привет", score: float = 0.95, *, fail: bool = False) -> None:
        self.text = text
        self.score = score
        self.fail = fail

    def predict(self, _image: object) -> list[object]:
        if self.fail:
            raise ValueError("failed")
        return [JsonResult({"rec_text": self.text, "rec_score": self.score})]


class FakePaddleModule:
    def __init__(self, payload: dict[str, object], *, fail_factory: bool = False) -> None:
        self.payload = payload
        self.fail_factory = fail_factory
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _make(self, name: str, values: dict[str, object]) -> object:
        self.calls.append((name, values))
        if self.fail_factory:
            raise ValueError("unsupported")
        return FakePipeline(self.payload)

    def PaddleOCR(self, **values: object) -> object:
        return self._make("text", values)

    def PPStructureV3(self, **values: object) -> object:
        return self._make("structured", values)

    def TextRecognition(self, **values: object) -> object:
        self.calls.append(("russian", values))
        if self.fail_factory:
            raise ValueError("unsupported")
        return FakeRussian()


def install_modules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: FakePaddleModule,
    *,
    valid_models: bool = True,
) -> None:
    monkeypatch.setattr("document_parser.paddle_ocr.resolve_model_store", lambda _target: tmp_path)
    monkeypatch.setattr(
        "document_parser.paddle_ocr.verify_ocr_models",
        lambda *_args, **_kwargs: OcrModelReport(
            target=tmp_path,
            valid=valid_models,
            missing=() if valid_models else ("missing",),
        ),
    )

    def fake_import(name: str) -> object:
        if name == "paddle":
            return SimpleNamespace(__version__="3")
        if name == "paddleocr":
            return module
        if name == "numpy":
            return SimpleNamespace(asarray=lambda value: value)
        raise ImportError(name)

    monkeypatch.setattr("document_parser.paddle_ocr.import_module", fake_import)


def text_payload(text: str = "Hello", score: float = 0.9) -> dict[str, object]:
    return {
        "rec_texts": [text],
        "rec_scores": [score],
        "rec_polys": [[(0, 0), (50, 0), (50, 10), (0, 10)]],
    }


def test_payload_helpers_accept_json_arrays_boxes_and_reject_unknown_values() -> None:
    payload = text_payload()
    assert _mapping({"res": payload}) == payload
    assert _mapping(JsonResult(payload))["rec_texts"] == payload["rec_texts"]
    with pytest.raises(OcrExecutionError, match="unsupported"):
        _mapping(object())
    assert _sequence(ArrayLike([1, 2])) == [1, 2]
    assert _sequence("text") == ()

    rectangle = _box((-1, -1, 120, 120), 100, 100)
    assert rectangle is not None
    assert rectangle.x == 0 and rectangle.width == 100
    polygon = _box(ArrayLike([ArrayLike([1, 2]), ArrayLike([5, 8])]), 10, 10)
    assert polygon is not None and polygon.width == 4
    assert _box(("bad",), 10, 10) is None

    recognized = _tokens(payload, 100, 100)
    assert recognized[0].text == "Hello"
    assert recognized[0].box.unit is CoordinateUnit.PIXEL
    assert _tokens({"rec_texts": ["", "missing box"]}, 100, 100) == ()


def test_language_selection_is_script_and_confidence_aware() -> None:
    latin = _Token("Hello", 0.9, pixel_box())
    selected, ambiguous = _choose_recognition(latin, "Привет", 0.89)
    assert selected.text == "Привет" and selected.language == "ru" and ambiguous

    selected, ambiguous = _choose_recognition(latin, "", 0)
    assert selected is latin and not ambiguous
    selected, _ = _choose_recognition(latin, "Bonjour", 0.99)
    assert selected.text == "Hello" and selected.language == "az-en"
    numeric = _Token("123", 0.5, pixel_box())
    selected, _ = _choose_recognition(numeric, "124", 0.9)
    assert selected.text == "124"


def test_table_geometry_and_label_mapping() -> None:
    tokens = (
        _Token("A", 0.9, pixel_box(0, 0, 40, 20)),
        _Token("B", 0.8, pixel_box(50, 0, 40, 20)),
    )
    table = _derive_table(
        {"cell_box_list": [(0, 0, 50, 25), (50, 0, 100, 25)]},
        tokens,
        100,
        100,
    )
    assert table is not None and table.column_count == 2
    assert [cell.text for cell in table.cells] == ["A", "B"]
    assert _derive_table({}, tokens, 100, 100) is None
    assert _derive_table({"cell_box_list": [(0, 0, 0, 0)]}, tokens, 100, 100) is None
    assert (
        _derive_table(
            {"cell_box_list": [(0, 0, 50, 25), (0, 0, 50, 25)]},
            tokens,
            100,
            100,
        )
        is None
    )
    assert _region_confidence((), 0.25) == 0.25
    assert _kind("doc_title") is OcrRegionKind.DOCUMENT_TITLE
    assert _kind("paragraph_title") is OcrRegionKind.PARAGRAPH_TITLE
    assert _kind("table") is OcrRegionKind.TABLE
    assert _kind("image") is OcrRegionKind.FIGURE
    assert _kind("caption") is OcrRegionKind.CAPTION
    assert _kind("list_item") is OcrRegionKind.LIST
    assert _kind("unknown") is OcrRegionKind.TEXT
    assert _looks_like_list("- item")
    assert _looks_like_list("2) item")
    assert not _looks_like_list("2)")
    assert not _looks_like_list("plain text")


def test_text_profile_builds_local_pipeline_and_reconciles_russian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = FakePaddleModule(text_payload("Privet", 0.7))
    install_modules(monkeypatch, tmp_path, module)
    options = OcrOptions(
        mode=OcrMode.AUTO,
        profile=OcrProfile.TEXT,
        model_store=tmp_path,
    )
    monkeypatch.setenv("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "original")
    engine = PaddleOcrEngine(options)
    assert __import__("os").environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] == "original"
    result = engine.recognize(page_input(), options)
    assert result.engine == "paddleocr"
    assert result.regions[0].lines[0].text == "Привет"
    assert result.ambiguous_language is False
    assert module.calls[0][0] == "text"
    assert module.calls[0][1]["device"] == "cpu"
    assert module.calls[0][1]["use_doc_unwarping"] is False


def test_structured_profile_maps_layout_tables_and_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload: dict[str, object] = {
        "overall_ocr_res": {
            "rec_texts": ["Report", "A", "B"],
            "rec_scores": [0.95, 0.9, 0.9],
            "rec_polys": [
                [(0, 0), (100, 0), (100, 15), (0, 15)],
                [(0, 25), (40, 25), (40, 40), (0, 40)],
                [(50, 25), (90, 25), (90, 40), (50, 40)],
            ],
        },
        "layout_det_res": {
            "boxes": [
                "invalid",
                {"label": "text", "coordinate": ["bad"]},
                {"label": "doc_title", "score": 0.99, "coordinate": [0, 0, 100, 20]},
                {"label": "table", "score": 0.9, "coordinate": [0, 20, 100, 50]},
                {"label": "figure", "score": 0.8, "coordinate": [0, 55, 100, 100]},
            ]
        },
        "table_res_list": [
            {
                "bbox": [0, 20, 100, 50],
                "cell_box_list": [[0, 20, 50, 50], [50, 20, 100, 50]],
            }
        ],
    }
    module = FakePaddleModule(payload)
    install_modules(monkeypatch, tmp_path, module)
    options = OcrOptions(
        mode=OcrMode.AUTO,
        profile=OcrProfile.STRUCTURED,
        languages=("az", "en"),
        model_store=tmp_path,
    )
    engine = PaddleOcrEngine(options)
    result = engine.recognize(page_input(), options)
    assert result.regions[0].kind is OcrRegionKind.DOCUMENT_TITLE
    assert result.regions[1].kind is OcrRegionKind.TABLE
    assert result.regions[1].table is not None
    assert result.regions[2].kind is OcrRegionKind.FIGURE
    assert module.calls[0][0] == "structured"
    assert module.calls[0][1]["use_formula_recognition"] is False

    fallback_payload = {
        **payload,
        "table_res_list": [],
    }
    fallback_module = FakePaddleModule(fallback_payload)
    install_modules(monkeypatch, tmp_path, fallback_module)
    fallback = PaddleOcrEngine(options).recognize(page_input(), options)
    assert fallback.regions[1].kind is OcrRegionKind.TEXT
    assert fallback.diagnostics[0].code == "ocr.table_unstructured"

    no_layout_module = FakePaddleModule(
        {
            **payload,
            "layout_det_res": {
                "boxes": [
                    "invalid",
                    {"coordinate": ["bad"]},
                    {"label": "text", "coordinate": [0, 90, 100, 100]},
                ]
            },
        }
    )
    install_modules(monkeypatch, tmp_path, no_layout_module)
    no_layout = PaddleOcrEngine(options).recognize(page_input(), options)
    assert no_layout.regions[0].kind is OcrRegionKind.TEXT

    empty_table_module = FakePaddleModule(
        {
            "overall_ocr_res": {},
            "layout_det_res": {
                "boxes": [{"label": "table", "score": 0.25, "coordinate": [0, 0, 100, 50]}]
            },
            "table_res_list": [{"bbox": [0, 0, 100, 50], "cell_box_list": [[0, 0, 100, 50]]}],
        }
    )
    install_modules(monkeypatch, tmp_path, empty_table_module)
    empty_table = PaddleOcrEngine(options).recognize(page_input(), options)
    assert empty_table.regions[0].confidence == 0.25


def test_engine_configuration_dependency_model_and_runtime_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = FakePaddleModule(text_payload())
    install_modules(monkeypatch, tmp_path, module)
    with pytest.raises(OcrConfigurationError, match="does not support"):
        PaddleOcrEngine(OcrOptions(languages=("de",), model_store=tmp_path))

    install_modules(monkeypatch, tmp_path, module, valid_models=False)
    with pytest.raises(OcrModelNotAvailableError, match="missing or invalid"):
        PaddleOcrEngine(OcrOptions(model_store=tmp_path))

    install_modules(monkeypatch, tmp_path, module)

    def missing(_name: str) -> object:
        raise ImportError("missing")

    monkeypatch.setattr("document_parser.paddle_ocr.import_module", missing)
    with pytest.raises(OcrDependencyNotAvailableError, match="PaddleOCR requires"):
        PaddleOcrEngine(OcrOptions(model_store=tmp_path))

    install_modules(monkeypatch, tmp_path, FakePaddleModule(text_payload(), fail_factory=True))
    with pytest.raises(OcrConfigurationError, match="local pipeline"):
        PaddleOcrEngine(
            OcrOptions(profile=OcrProfile.TEXT, languages=("az",), model_store=tmp_path)
        )
    with pytest.raises(OcrConfigurationError, match="local pipeline"):
        PaddleOcrEngine(
            OcrOptions(profile=OcrProfile.STRUCTURED, languages=("az",), model_store=tmp_path)
        )

    install_modules(monkeypatch, tmp_path, module)
    options = OcrOptions(profile=OcrProfile.TEXT, languages=("az",), model_store=tmp_path)
    engine = PaddleOcrEngine(options)
    with pytest.raises(OcrConfigurationError, match="differ"):
        engine.recognize(page_input(), options.model_copy(update={"dpi": 200}))
    engine._pipeline = object()
    with pytest.raises(OcrExecutionError, match="predict"):
        engine.recognize(page_input(), options)

    class BrokenPipeline:
        def predict(self, _image: object) -> list[object]:
            raise ValueError("backend crash")

    engine._pipeline = BrokenPipeline()
    with pytest.raises(OcrExecutionError, match="page inference failed"):
        engine.recognize(page_input(), options)


def test_russian_factory_and_inference_failures_are_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = FakePaddleModule(text_payload())
    install_modules(monkeypatch, tmp_path, module)
    options = OcrOptions(profile=OcrProfile.TEXT, model_store=tmp_path)
    engine = PaddleOcrEngine(options)
    engine._russian = FakeRussian(fail=True)
    with pytest.raises(OcrExecutionError, match="Russian"):
        engine.recognize(page_input(), options)

    class BrokenRussianModule(FakePaddleModule):
        def TextRecognition(self, **values: object) -> object:
            raise ValueError(values)

    install_modules(monkeypatch, tmp_path, BrokenRussianModule(text_payload()))
    with pytest.raises(OcrConfigurationError, match="Russian recognizer"):
        PaddleOcrEngine(options)


def test_empty_pipeline_result_returns_empty_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = FakePaddleModule({})
    install_modules(monkeypatch, tmp_path, module)
    options = OcrOptions(profile=OcrProfile.TEXT, languages=("az",), model_store=tmp_path)
    engine = PaddleOcrEngine(options)
    engine._pipeline = FakePipeline({}, empty=True)
    result = engine.recognize(page_input(), options)
    assert result.regions == ()
