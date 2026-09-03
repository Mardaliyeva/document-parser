"""Public inspection and parsing orchestration."""

from __future__ import annotations

from collections.abc import Iterable

from document_parser.adapters import AdapterRegistry, DocumentAdapter
from document_parser.exceptions import (
    AdapterExecutionError,
    AdapterNotAvailableError,
    DocumentParserError,
)
from document_parser.models import Document, SourceInfo
from document_parser.sources import ParseOptions, SourceInput, prepare_source


class DocumentParser:
    """Parser configuration and immutable format-adapter registry."""

    __slots__ = ("_registry", "options")

    def __init__(
        self,
        *,
        options: ParseOptions | None = None,
        adapters: Iterable[DocumentAdapter] = (),
    ) -> None:
        self.options = options or ParseOptions()
        self._registry = AdapterRegistry(adapters)

    @property
    def supported_formats(self) -> tuple[str, ...]:
        """Formats that can currently be parsed by this instance."""

        return tuple(document_format.value for document_format in self._registry.formats)

    def inspect(self, source: SourceInput, *, filename: str | None = None) -> SourceInfo:
        """Validate an input and return content-derived source information."""

        with prepare_source(source, filename=filename, options=self.options) as prepared:
            return prepared.info

    def parse(self, source: SourceInput, *, filename: str | None = None) -> Document:
        """Parse an input with the adapter selected from its detected content."""

        with prepare_source(source, filename=filename, options=self.options) as prepared:
            adapter = self._registry.get(prepared.info.format)
            if adapter is None:
                raise AdapterNotAvailableError(
                    f"no adapter is registered for {prepared.info.format.value}",
                    source_name=prepared.info.name,
                )
            try:
                document = adapter.parse(prepared, self.options)
            except DocumentParserError:
                raise
            except Exception as exc:
                raise AdapterExecutionError(
                    f"{prepared.info.format.value} adapter failed",
                    source_name=prepared.info.name,
                ) from exc
            if not isinstance(document, Document):
                raise AdapterExecutionError(
                    "adapter did not return a Document",
                    source_name=prepared.info.name,
                )
            if document.source != prepared.info:
                raise AdapterExecutionError(
                    "adapter returned a Document for a different source",
                    source_name=prepared.info.name,
                )
            return document


def inspect_source(
    source: SourceInput,
    *,
    filename: str | None = None,
    options: ParseOptions | None = None,
) -> SourceInfo:
    """Inspect one source using the default adapter-free parser."""

    return DocumentParser(options=options).inspect(source, filename=filename)


def parse(
    source: SourceInput,
    *,
    filename: str | None = None,
    options: ParseOptions | None = None,
) -> Document:
    """Parse one source using the default parser and its built-in adapters."""

    return DocumentParser(options=options).parse(source, filename=filename)
