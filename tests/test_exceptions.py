"""Tests for stable exception codes and safe string rendering."""

from __future__ import annotations

import pytest

from document_parser import (
    AdapterExecutionError,
    AdapterNotAvailableError,
    DocumentParserError,
    ErrorCode,
    InvalidDocumentError,
    OcrConfigurationError,
    OcrDependencyNotAvailableError,
    OcrExecutionError,
    OcrModelNotAvailableError,
    SourceNotFoundError,
    SourceReadError,
    SourceTooLargeError,
    UnsafeDocumentError,
    UnsupportedFormatError,
)


@pytest.mark.parametrize(
    ("exception_type", "code"),
    [
        (SourceNotFoundError, ErrorCode.SOURCE_NOT_FOUND),
        (SourceReadError, ErrorCode.SOURCE_READ_ERROR),
        (SourceTooLargeError, ErrorCode.SOURCE_TOO_LARGE),
        (UnsupportedFormatError, ErrorCode.UNSUPPORTED_FORMAT),
        (InvalidDocumentError, ErrorCode.INVALID_DOCUMENT),
        (UnsafeDocumentError, ErrorCode.UNSAFE_DOCUMENT),
        (AdapterNotAvailableError, ErrorCode.ADAPTER_NOT_AVAILABLE),
        (AdapterExecutionError, ErrorCode.ADAPTER_EXECUTION_ERROR),
        (OcrDependencyNotAvailableError, ErrorCode.OCR_DEPENDENCY_NOT_AVAILABLE),
        (OcrModelNotAvailableError, ErrorCode.OCR_MODEL_NOT_AVAILABLE),
        (OcrConfigurationError, ErrorCode.OCR_CONFIGURATION_ERROR),
        (OcrExecutionError, ErrorCode.OCR_EXECUTION_ERROR),
    ],
)
def test_exception_code_contract(
    exception_type: type[DocumentParserError], code: ErrorCode
) -> None:
    error = exception_type("safe message", source_name="document.pdf")
    assert error.code is code
    assert error.message == "safe message"
    assert str(error) == "safe message (source: document.pdf)"


def test_exception_without_source_renders_only_message() -> None:
    error = SourceReadError("safe message")
    assert str(error) == "safe message"
