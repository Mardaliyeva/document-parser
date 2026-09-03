"""Typed exceptions raised by the document parsing pipeline."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable machine-readable error codes."""

    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_READ_ERROR = "source_read_error"
    SOURCE_TOO_LARGE = "source_too_large"
    UNSUPPORTED_FORMAT = "unsupported_format"
    INVALID_DOCUMENT = "invalid_document"
    UNSAFE_DOCUMENT = "unsafe_document"
    ADAPTER_NOT_AVAILABLE = "adapter_not_available"
    ADAPTER_EXECUTION_ERROR = "adapter_execution_error"


class DocumentParserError(Exception):
    """Base class for expected document-parser failures."""

    code: ErrorCode

    def __init__(self, message: str, *, source_name: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.source_name = source_name

    def __str__(self) -> str:
        if self.source_name is None:
            return self.message
        return f"{self.message} (source: {self.source_name})"


class SourceNotFoundError(DocumentParserError):
    """Raised when an input path does not exist."""

    code = ErrorCode.SOURCE_NOT_FOUND


class SourceReadError(DocumentParserError):
    """Raised when an input cannot be read as binary data."""

    code = ErrorCode.SOURCE_READ_ERROR


class SourceTooLargeError(DocumentParserError):
    """Raised when the configured input-size limit is exceeded."""

    code = ErrorCode.SOURCE_TOO_LARGE


class UnsupportedFormatError(DocumentParserError):
    """Raised when input bytes do not represent a supported format."""

    code = ErrorCode.UNSUPPORTED_FORMAT


class InvalidDocumentError(DocumentParserError):
    """Raised when a claimed supported document is malformed."""

    code = ErrorCode.INVALID_DOCUMENT


class UnsafeDocumentError(DocumentParserError):
    """Raised when a document violates a preflight safety rule."""

    code = ErrorCode.UNSAFE_DOCUMENT


class AdapterNotAvailableError(DocumentParserError):
    """Raised when a detected format has no registered adapter."""

    code = ErrorCode.ADAPTER_NOT_AVAILABLE


class AdapterExecutionError(DocumentParserError):
    """Raised when an adapter violates its contract or fails unexpectedly."""

    code = ErrorCode.ADAPTER_EXECUTION_ERROR
