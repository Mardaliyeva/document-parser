"""Public inspection and parsing orchestration."""

from __future__ import annotations

from collections.abc import Iterable

from document_parser.adapters import AdapterRegistry, DocumentAdapter, builtin_adapters
from document_parser.exceptions import (
    AdapterExecutionError,
    AdapterNotAvailableError,
    DocumentParserError,
)
from document_parser.markdown import MarkdownOptions, to_markdown
from document_parser.models import Document, SourceInfo
from document_parser.ocr import OcrEngine, apply_pdf_ocr
from document_parser.results import AdapterOutput, ConversionResult
from document_parser.sources import ParseOptions, SourceInput, prepare_source


class DocumentParser:
    """Parser configuration and immutable format-adapter registry."""

    __slots__ = ("_ocr_engine", "_registry", "options")

    def __init__(
        self,
        *,
        options: ParseOptions | None = None,
        adapters: Iterable[DocumentAdapter] | None = None,
        ocr_engine: OcrEngine | None = None,
    ) -> None:
        self.options = options or ParseOptions()
        self._registry = AdapterRegistry(builtin_adapters() if adapters is None else adapters)
        self._ocr_engine = ocr_engine

    @property
    def supported_formats(self) -> tuple[str, ...]:
        """Formats that can currently be parsed by this instance."""

        return tuple(document_format.value for document_format in self._registry.formats)

    def inspect(self, source: SourceInput, *, filename: str | None = None) -> SourceInfo:
        """Validate an input and return content-derived source information."""

        with prepare_source(source, filename=filename, options=self.options) as prepared:
            return prepared.info

    def _parse_output(self, source: SourceInput, *, filename: str | None) -> AdapterOutput:
        with prepare_source(source, filename=filename, options=self.options) as prepared:
            adapter = self._registry.get(prepared.info.format)
            if adapter is None:
                raise AdapterNotAvailableError(
                    f"no adapter is registered for {prepared.info.format.value}",
                    source_name=prepared.info.name,
                )
            try:
                output = adapter.parse(prepared, self.options)
            except DocumentParserError:
                raise
            except Exception as exc:
                raise AdapterExecutionError(
                    f"{prepared.info.format.value} adapter failed",
                    source_name=prepared.info.name,
                ) from exc
            if not isinstance(output, AdapterOutput):
                raise AdapterExecutionError(
                    "adapter did not return an AdapterOutput",
                    source_name=prepared.info.name,
                )
            if output.document.source != prepared.info:
                raise AdapterExecutionError(
                    "adapter returned a Document for a different source",
                    source_name=prepared.info.name,
                )
            output, self._ocr_engine = apply_pdf_ocr(
                prepared,
                output,
                self.options,
                self._ocr_engine,
            )
            return output

    def parse(self, source: SourceInput, *, filename: str | None = None) -> Document:
        """Parse an input and return its format-independent Document IR."""

        return self._parse_output(source, filename=filename).document

    def convert(
        self,
        source: SourceInput,
        *,
        filename: str | None = None,
        markdown_options: MarkdownOptions | None = None,
    ) -> ConversionResult:
        """Parse one input and return IR, Markdown, and extracted assets."""

        output = self._parse_output(source, filename=filename)
        return ConversionResult(
            document=output.document,
            markdown=to_markdown(output.document, options=markdown_options),
            assets=output.assets,
        )


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


def convert(
    source: SourceInput,
    *,
    filename: str | None = None,
    options: ParseOptions | None = None,
    markdown_options: MarkdownOptions | None = None,
) -> ConversionResult:
    """Parse one source with built-ins and return a complete conversion bundle."""

    return DocumentParser(options=options).convert(
        source,
        filename=filename,
        markdown_options=markdown_options,
    )
