"""Smoke tests for the package's initial public surface."""

import sys

import document_parser


def test_package_import_exposes_version_only() -> None:
    assert document_parser.__version__ == "0.2.0a1"
    assert document_parser.SCHEMA_VERSION == "0.1"
    assert {
        "Document",
        "DocumentParser",
        "ContentBlock",
        "ParseOptions",
        "inspect_source",
        "parse",
    }.issubset(document_parser.__all__)


def test_import_does_not_load_document_engines() -> None:
    forbidden_names = {
        "docling",
        "openpyxl",
        "paddleocr",
        "pypdfium2",
        "python_docx",
    }
    assert forbidden_names.isdisjoint(vars(document_parser))
    assert forbidden_names.isdisjoint(sys.modules)
