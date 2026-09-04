"""Tests for installed distribution metadata and typing marker."""

from importlib import resources
from importlib.metadata import entry_points, metadata, version

import document_parser


def test_distribution_metadata_matches_package() -> None:
    package_metadata = metadata("document-parser")

    assert package_metadata["Name"] == "document-parser"
    assert set(package_metadata["Requires-Python"].split(",")) == {">=3.11", "<3.13"}
    assert package_metadata["License-Expression"] == "Apache-2.0"
    assert version("document-parser") == document_parser.__version__
    requirements = package_metadata.get_all("Requires-Dist") or []
    required_names = {
        requirement.split(" ", maxsplit=1)[0].split("<", maxsplit=1)[0].split(">", maxsplit=1)[0]
        for requirement in requirements
        if "extra ==" not in requirement
    }
    assert {
        "defusedxml",
        "lxml",
        "openpyxl",
        "pdfplumber",
        "pillow",
        "pydantic",
        "pypdf",
        "python-docx",
    }.issubset(required_names)
    ocr_requirements = {
        requirement
        for requirement in requirements
        if "extra ==" in requirement and "ocr" in requirement
    }
    assert any(requirement.startswith("paddleocr") for requirement in ocr_requirements)
    assert any(requirement.startswith("platformdirs") for requirement in ocr_requirements)
    assert any(requirement.startswith("pypdfium2") for requirement in ocr_requirements)
    scripts = entry_points(group="console_scripts")
    assert any(
        item.name == "document-parser" and item.value == "document_parser.cli:main"
        for item in scripts
    )


def test_typing_marker_is_packaged() -> None:
    marker = resources.files("document_parser").joinpath("py.typed")
    assert marker.is_file()
