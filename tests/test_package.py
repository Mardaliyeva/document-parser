"""Smoke tests for the package's initial public surface."""

import document_parser


def test_package_import_exposes_version_only() -> None:
    assert document_parser.__version__ == "0.1.0a1"
    assert document_parser.__all__ == ["__version__"]


def test_import_does_not_load_document_engines() -> None:
    forbidden_names = {"docling", "openpyxl", "paddleocr", "pypdfium2"}
    assert forbidden_names.isdisjoint(vars(document_parser))
