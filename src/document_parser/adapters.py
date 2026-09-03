"""Adapter protocol and immutable registry."""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from document_parser.models import DocumentFormat
from document_parser.results import AdapterOutput
from document_parser.sources import AdapterInput, ParseOptions


@runtime_checkable
class DocumentAdapter(Protocol):
    """Contract implemented by every format-specific parser."""

    format: DocumentFormat

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        """Parse a prepared source into the common document IR."""


def builtin_adapters() -> tuple[DocumentAdapter, ...]:
    """Construct built-in adapters without importing their heavy engines."""

    from document_parser.docx_adapter import DocxAdapter
    from document_parser.pdf_adapter import PdfAdapter
    from document_parser.xlsx_adapter import XlsxAdapter

    return (DocxAdapter(), PdfAdapter(), XlsxAdapter())


class AdapterRegistry:
    """Read-only mapping of one adapter per document format."""

    __slots__ = ("_adapters",)

    def __init__(self, adapters: Iterable[DocumentAdapter] = ()) -> None:
        registered: dict[DocumentFormat, DocumentAdapter] = {}
        for adapter in adapters:
            document_format = adapter.format
            if not isinstance(document_format, DocumentFormat):
                raise ValueError("adapter.format must be a DocumentFormat")
            if document_format in registered:
                raise ValueError(f"duplicate adapter for format: {document_format.value}")
            registered[document_format] = adapter
        self._adapters = MappingProxyType(registered)

    @property
    def formats(self) -> tuple[DocumentFormat, ...]:
        """Registered formats in deterministic order."""

        return tuple(sorted(self._adapters, key=lambda item: item.value))

    def get(self, document_format: DocumentFormat) -> DocumentAdapter | None:
        """Return the adapter registered for a format, if any."""

        return self._adapters.get(document_format)
