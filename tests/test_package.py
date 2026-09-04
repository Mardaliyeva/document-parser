"""Smoke tests for the package's initial public surface."""

import subprocess
import sys

import document_parser


def test_package_import_exposes_version_only() -> None:
    assert document_parser.__version__ == "0.4.0a1"
    assert document_parser.SCHEMA_VERSION == "0.1"
    assert {
        "Document",
        "DocumentParser",
        "ConversionResult",
        "ContentBlock",
        "ParseOptions",
        "inspect_source",
        "convert",
        "parse",
        "to_markdown",
    }.issubset(document_parser.__all__)


def test_import_does_not_load_document_engines() -> None:
    forbidden_names = {
        "docling",
        "openpyxl",
        "paddleocr",
        "pypdfium2",
        "docx",
        "pdfplumber",
        "pypdf",
    }
    script = (
        "import sys; import document_parser; "
        f"forbidden={forbidden_names!r}; "
        "assert forbidden.isdisjoint(vars(document_parser)); "
        "assert forbidden.isdisjoint(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", script], check=True)
