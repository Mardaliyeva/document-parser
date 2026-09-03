"""Tests for adapter registration and public parsing orchestration."""

from __future__ import annotations

from typing import cast

import pytest

from document_parser import (
    AdapterExecutionError,
    AdapterInput,
    AdapterNotAvailableError,
    AdapterOutput,
    AdapterRegistry,
    Document,
    DocumentAdapter,
    DocumentFormat,
    DocumentParser,
    InvalidDocumentError,
    ParagraphBlock,
    ParseOptions,
    SourceReadError,
    TextSpan,
)

PDF_BYTES = b"%PDF-1.7\n%%EOF\n"


def document_for(source: AdapterInput) -> Document:
    return Document(
        document_id=f"sha256:{source.info.sha256}",
        source=source.info,
        blocks=(
            ParagraphBlock(
                block_id="p-000001",
                spans=(TextSpan(text="Synthetic adapter output"),),
            ),
        ),
    )


class SuccessfulAdapter:
    format = DocumentFormat.PDF

    def __init__(self) -> None:
        self.captured: AdapterInput | None = None
        self.payloads: list[bytes] = []
        self.rolled_to_disk: list[bool] = []
        self.options: ParseOptions | None = None

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        self.captured = source
        self.options = options
        with source.open_binary() as stream:
            self.rolled_to_disk.append(bool(getattr(stream, "_rolled", False)))
            self.payloads.append(stream.read())
        with source.open_binary() as stream:
            self.payloads.append(stream.read())
        return AdapterOutput(document=document_for(source))


class DocxAdapter(SuccessfulAdapter):
    format = DocumentFormat.DOCX


class RaisingAdapter:
    format = DocumentFormat.PDF

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        raise InvalidDocumentError("adapter rejected input", source_name=source.info.name)


class ExplodingAdapter:
    format = DocumentFormat.PDF

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        raise ValueError("private implementation detail")


class WrongTypeAdapter:
    format = DocumentFormat.PDF

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        return "not a document"  # type: ignore[return-value]


class WrongSourceAdapter:
    format = DocumentFormat.PDF

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        wrong_source = source.info.model_copy(update={"name": "other.pdf"})
        return AdapterOutput(
            document=Document(
                document_id=f"sha256:{source.info.sha256}",
                source=wrong_source,
            )
        )


class ClosingAdapter:
    format = DocumentFormat.PDF

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        with source.open_binary() as stream:
            stream.close()
        return AdapterOutput(document=document_for(source))


class InvalidFormatAdapter:
    format = "pdf"

    def parse(self, source: AdapterInput, options: ParseOptions) -> AdapterOutput:
        return AdapterOutput(document=document_for(source))


def test_registry_is_immutable_sorted_and_protocol_compatible() -> None:
    pdf_adapter = SuccessfulAdapter()
    docx_adapter = DocxAdapter()
    registry = AdapterRegistry((pdf_adapter, docx_adapter))

    assert isinstance(pdf_adapter, DocumentAdapter)
    assert registry.formats == (DocumentFormat.DOCX, DocumentFormat.PDF)
    assert registry.get(DocumentFormat.PDF) is pdf_adapter
    assert registry.get(DocumentFormat.XLSX) is None


def test_registry_rejects_duplicate_and_invalid_formats() -> None:
    with pytest.raises(ValueError, match="duplicate adapter"):
        AdapterRegistry((SuccessfulAdapter(), SuccessfulAdapter()))

    invalid = cast(DocumentAdapter, InvalidFormatAdapter())
    with pytest.raises(ValueError, match="must be a DocumentFormat"):
        AdapterRegistry((invalid,))


def test_synthetic_adapter_runs_full_pipeline_deterministically() -> None:
    adapter = SuccessfulAdapter()
    options = ParseOptions()
    parser = DocumentParser(options=options, adapters=(adapter,))

    first = parser.parse(PDF_BYTES, filename="sample.pdf")
    second = parser.parse(PDF_BYTES, filename="sample.pdf")

    assert first == second
    assert parser.supported_formats == ("pdf",)
    assert adapter.options is options
    assert adapter.payloads == [PDF_BYTES, PDF_BYTES, PDF_BYTES, PDF_BYTES]
    assert adapter.captured is not None
    assert adapter.captured.closed
    with pytest.raises(SourceReadError, match="already closed"), adapter.captured.open_binary():
        pass


def test_input_larger_than_spool_threshold_rolls_to_disk_and_is_cleaned() -> None:
    adapter = SuccessfulAdapter()
    parser = DocumentParser(
        options=ParseOptions(max_input_bytes=1024, spool_threshold_bytes=1),
        adapters=(adapter,),
    )

    parser.parse(PDF_BYTES, filename="spooled.pdf")

    assert adapter.rolled_to_disk == [True]
    assert adapter.captured is not None
    assert adapter.captured.closed


def test_inspect_succeeds_without_an_adapter_but_parse_does_not() -> None:
    parser = DocumentParser(adapters=())
    assert parser.inspect(PDF_BYTES, filename="sample.pdf").format is DocumentFormat.PDF
    assert parser.supported_formats == ()

    with pytest.raises(AdapterNotAvailableError, match="no adapter") as error:
        parser.parse(PDF_BYTES, filename="sample.pdf")
    assert error.value.source_name == "sample.pdf"

    assert DocumentParser().supported_formats == ("docx", "pdf", "xlsx")


def test_expected_adapter_error_is_not_wrapped() -> None:
    parser = DocumentParser(adapters=(RaisingAdapter(),))
    with pytest.raises(InvalidDocumentError, match="adapter rejected"):
        parser.parse(PDF_BYTES, filename="sample.pdf")


def test_unexpected_adapter_error_is_wrapped_without_leaking_message() -> None:
    parser = DocumentParser(adapters=(ExplodingAdapter(),))
    with pytest.raises(AdapterExecutionError, match="pdf adapter failed") as error:
        parser.parse(PDF_BYTES, filename="sample.pdf")
    assert isinstance(error.value.__cause__, ValueError)
    assert "private implementation detail" not in str(error.value)


@pytest.mark.parametrize(
    ("adapter", "message"),
    [
        (WrongTypeAdapter(), "did not return"),
        (WrongSourceAdapter(), "different source"),
    ],
)
def test_adapter_contract_is_checked(adapter: DocumentAdapter, message: str) -> None:
    parser = DocumentParser(adapters=(adapter,))
    with pytest.raises(AdapterExecutionError, match=message):
        parser.parse(PDF_BYTES, filename="sample.pdf")


def test_adapter_may_close_borrowed_stream_without_cleanup_failure() -> None:
    parser = DocumentParser(adapters=(ClosingAdapter(),))
    document = parser.parse(PDF_BYTES, filename="sample.pdf")
    assert document.source.name == "sample.pdf"
