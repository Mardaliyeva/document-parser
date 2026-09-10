"""Opt-in smoke tests for the real local PaddleOCR runtime and prepared models."""

from __future__ import annotations

import io
import os
import socket
import unicodedata
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from document_parser import (
    DocumentParser,
    OcrMode,
    OcrOptions,
    OcrPageInput,
    OcrProfile,
    OcrRegionKind,
    ParseOptions,
)
from document_parser.ocr import _render_page
from document_parser.paddle_ocr import PaddleOcrEngine

pytestmark = [
    pytest.mark.ocr_integration,
    pytest.mark.skipif(
        os.environ.get("DOCUMENT_PARSER_RUN_OCR_INTEGRATION") != "1",
        reason="real OCR integration is opt-in",
    ),
]

_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/calibri.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
)
_SYNTHETIC_SCAN = Path(__file__).parent / "fixtures" / "synthetic" / "scanned-image-only.pdf"


@pytest.fixture(autouse=True)
def forbid_network_during_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prepared-model inference must not fall back to Python network downloads."""

    def denied(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted during offline OCR")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in _FONT_CANDIDATES:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    pytest.fail("no Unicode test font is available on this runner")


def _page(image: Image.Image) -> OcrPageInput:
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return OcrPageInput(
        page_number=1,
        image_png=stream.getvalue(),
        width_pixels=image.width,
        height_pixels=image.height,
        source_width_points=image.width / 300 * 72,
        source_height_points=image.height / 300 * 72,
        dpi=300,
        rotation=0,
    )


def _model_store() -> Path:
    value = os.environ.get("DOCUMENT_PARSER_OCR_MODEL_STORE")
    if not value:
        pytest.fail("DOCUMENT_PARSER_OCR_MODEL_STORE is required")
    return Path(value)


def test_real_text_profile_recognizes_az_en_and_ru() -> None:
    options = OcrOptions(
        mode=OcrMode.FORCE,
        profile=OcrProfile.TEXT,
        model_store=_model_store(),
        languages=("az", "en", "ru"),
    )
    pdfium = pytest.importorskip("pypdfium2")
    pdf = pdfium.PdfDocument(_SYNTHETIC_SCAN.read_bytes())
    try:
        page = _render_page(pdf, 1, 0, options)
    finally:
        pdf.close()
    result = PaddleOcrEngine(options).recognize(page, options)
    text = " ".join(region.text for region in result.regions).casefold()
    assert "english" in text
    assert any(character in text for character in "əğıöüşç")
    assert any("\u0430" <= character <= "\u044f" or character == "\u0451" for character in text)


def test_real_structured_profile_recovers_a_simple_table() -> None:
    image = Image.new("RGB", (1800, 1100), "white")
    draw = ImageDraw.Draw(image)
    font = _font(54)
    left, top, width, row_height = 100, 100, 1500, 260
    for row in range(4):
        y = top + row * row_height
        draw.line((left, y, left + width, y), fill="black", width=8)
    for column in range(3):
        x = left + column * width // 2
        draw.line((x, top, x, top + 3 * row_height), fill="black", width=8)
    values = (("Name", "Value"), ("Alpha", "10"), ("Beta", "20"))
    for row, cells in enumerate(values):
        for column, value in enumerate(cells):
            draw.text(
                (left + 35 + column * width // 2, top + 70 + row * row_height),
                value,
                fill="black",
                font=font,
            )
    options = OcrOptions(
        mode=OcrMode.FORCE,
        profile=OcrProfile.STRUCTURED,
        model_store=_model_store(),
        languages=("az", "en", "ru"),
    )
    result = PaddleOcrEngine(options).recognize(_page(image), options)
    tables = tuple(region for region in result.regions if region.kind is OcrRegionKind.TABLE)
    assert tables
    assert tables[0].table is not None
    assert tables[0].table.row_count >= 2
    assert tables[0].table.column_count >= 2
    assert tables[0].table.row_count == 3
    assert tables[0].table.column_count == 2
    assert [cell.text for cell in tables[0].table.cells] == [
        "Name",
        "Value",
        "Alpha",
        "10",
        "Beta",
        "20",
    ]


def test_real_ocr_runs_through_the_full_parser_pipeline() -> None:
    options = OcrOptions(
        mode=OcrMode.AUTO,
        profile=OcrProfile.TEXT,
        model_store=_model_store(),
        languages=("az", "en", "ru"),
    )
    result = DocumentParser(options=ParseOptions(ocr=options)).convert(_SYNTHETIC_SCAN)
    text = result.markdown.casefold()

    assert "english" in text
    assert any(character in text for character in "əğıöüşç")
    assert any("\u0430" <= character <= "\u044f" or character == "\u0451" for character in text)
    assert any(item.code == "ocr.applied" for item in result.document.diagnostics)
    assert not any(item.code == "pdf.ocr_required" for item in result.document.diagnostics)


@pytest.mark.parametrize("profile", (OcrProfile.TEXT, OcrProfile.STRUCTURED))
def test_real_printed_reference_character_error_rate(profile: OcrProfile) -> None:
    """Measured printed reference, not a language-presence smoke assertion."""
    lines = (
        "Azərbaycan sənədi: ə ğ \u0131 ö ü ş ç.",
        "Document number: TEST-42. Date: 2026-09-08.",
        "Русский документ. Проверка данных.",
    )
    image = Image.new("RGB", (2200, 600), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((80, 70 + index * 150), line, fill="black", font=_font(60))
    options = OcrOptions(mode=OcrMode.FORCE, profile=profile, model_store=_model_store())
    result = PaddleOcrEngine(options).recognize(_page(image), options)
    expected = " ".join(lines)
    actual = " ".join(region.text for region in result.regions)
    actual = " ".join(unicodedata.normalize("NFC", actual).split())
    previous = list(range(len(actual) + 1))
    for index, reference in enumerate(expected, start=1):
        current = [index]
        for column, recognized in enumerate(actual, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (reference != recognized),
                )
            )
        previous = current
    cer = previous[-1] / len(expected)
    print(f"CER={cer:.6f}; reference={expected!a}; actual={actual!a}")
    assert cer <= 0.02
    assert "TEST-42" in actual and "2026-09-08" in actual
