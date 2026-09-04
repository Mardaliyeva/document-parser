# document-parser

`document-parser` is a local-first Python library for converting documents into
structure-preserving data suitable for Markdown generation and downstream RAG
workflows.

> **Status:** pre-alpha. Native DOCX/PDF/XLSX conversion and opt-in local OCR for
> scanned or mixed PDFs are available. OCR is disabled by default and never uses
> a cloud service.

## Supported input

| Format | Extraction path | Current behavior |
| --- | --- | --- |
| DOCX | `python-docx` plus bounded OOXML reads | Headings, spans, links, lists, tables, sections, stories, page breaks, and images |
| PDF | Native extraction plus optional `pypdfium2`/PaddleOCR | Native layout, scan detection, selective page OCR, coordinates, headings, lists, and tables |
| XLSX | `openpyxl` plus `defusedxml` protection | Sheets, regions, formal tables, formulas, displayed values, merges, visibility, and images |

Legacy Office files, macro-enabled packages, encrypted Office documents, and
encrypted PDFs are rejected. Excel formulas are preserved but never evaluated.
External relationships and images are never fetched from the internet.

The project does not use cloud services, execute macros, create embeddings, or
write to a vector database.

## Project principles

- Local processing by default, with no document telemetry.
- Structure and factual fidelity before cosmetic cleanup.
- A lossless JSON intermediate representation as the source of truth.
- Deterministic outputs for fixed input, options, and model versions.
- Selective OCR instead of OCRing every page or embedded image.
- Permissively licensed dependencies and model assets only.

## Core API

`convert()` returns one immutable `ConversionResult` containing the Document IR,
canonical Markdown, and validated content-addressed binary assets:

```python
from pathlib import Path

from document_parser import convert

result = convert("contract.docx")

print(result.markdown)
print(result.document.status)

asset_directory = Path("assets")
asset_directory.mkdir(exist_ok=True)
for asset in result.assets:
    (asset_directory / asset.ref.filename).write_bytes(asset.data)
```

Use `parse()` when only the loss-preserving IR is needed and `to_markdown()` when
an existing `Document` should be serialized separately:

```python
from document_parser import MarkdownOptions, parse, to_markdown

document = parse(pdf_bytes, filename="report.pdf")
markdown = to_markdown(
    document,
    options=MarkdownOptions(include_source_markers=True),
)
```

Paths, bytes, byte arrays, and binary streams are accepted. A string is always a
path, not raw document text. Caller-owned streams are not closed, and seekable
stream positions are restored. Detection uses file content rather than trusting
the extension.

`DocumentParser()` installs the three built-in adapters. Passing `adapters=[]`
creates an adapter-free parser; a custom adapter list replaces the built-ins.
Duplicate format registrations fail immediately.

```python
from document_parser import DocumentParser, ParseOptions, PdfOptions, XlsxOptions

parser = DocumentParser(
    options=ParseOptions(
        pdf=PdfOptions(min_native_alphanumeric_chars=20),
        xlsx=XlsxOptions(include_hidden_sheets=False),
    )
)

source = parser.inspect("workbook.xlsx")
document = parser.parse("workbook.xlsx")
result = parser.convert("workbook.xlsx")
```

Input, archive, worksheet, and asset limits are enforced before or during
extraction. Recoverable fidelity losses produce diagnostics and `partial`; an
OCR candidate produces `needs_review` while preserving available native content.

The dependency-free Markdown serializer preserves hierarchy, emits simple
tables as GFM and merge-aware tables as HTML, uses content-addressed asset links,
hides repeated headers/footers and hidden sheets by default, and marks PDF pages
with `<!-- page: N -->`. Metadata, coordinates, raw spreadsheet values, formulas,
diagnostics, and hidden IR content remain available on `result.document`.

## Optional local OCR

The base install remains OCR-free. Install the OCR extra and then install a
PaddlePaddle 3.x CPU or GPU runtime appropriate for the target platform, using
the [official PaddlePaddle installation instructions](https://www.paddleocr.ai/latest/en/version3.x/paddlepaddle_installation.html):

```powershell
.venv\Scripts\python -m pip install -e ".[ocr]"
```

Model downloads are a separate, explicit operation. They never happen inside
`parse()` or `convert()`:

```python
from document_parser import OcrProfile, prepare_ocr_models

prepare_ocr_models(
    "./models",
    profiles=(OcrProfile.STRUCTURED,),
    languages=("az", "en", "ru"),
)
```

After preparation, conversion is local and offline:

```python
from pathlib import Path

from document_parser import OcrMode, OcrOptions, ParseOptions, convert

options = ParseOptions(
    ocr=OcrOptions(
        mode=OcrMode.AUTO,
        model_store=Path("models"),
        languages=("az", "en", "ru"),
    )
)
result = convert("mixed-or-scanned.pdf", options=options)
```

`AUTO` processes only pages marked as scan candidates by native PDF analysis;
`FORCE` processes every non-empty PDF page; `OFF` is the default. The
`STRUCTURED` profile restores layout and tables, while `TEXT` is lighter and
does not promise table structure. OCR shadow/native blocks remain in the IR but
are excluded from Markdown after a successful OCR replacement. A no-text OCR
result keeps native content active as a safe fallback.

See [OCR setup and behavior](docs/ocr.md) for model-store security, custom
engines, limits, diagnostics, and reproducibility details.

## Development setup

Create a Python 3.11 or 3.12 virtual environment and install the development
dependencies:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

On Linux or macOS, activate the equivalent virtual environment and use its
`python` executable.

Run the local checks:

```powershell
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy src tests
.venv\Scripts\python -m pytest
.venv\Scripts\python -m build
.venv\Scripts\python -m twine check dist\*
```

CI tests Python 3.11/3.12 on Windows and Linux, verifies minimum dependency
versions, enforces 100% branch coverage and the runtime-license allowlist, and
creates a reproducible CycloneDX SBOM.

## Roadmap

1. **Complete:** package, policy, CI, and release scaffold.
2. **Complete:** Document IR, safe input preparation, detection, and routing.
3. **Complete:** native DOCX/PDF/XLSX adapters and canonical Markdown.
4. **Complete:** opt-in selective local OCR for scanned and mixed PDF pages.
5. Add native/OCR reconciliation, structural normalization, and quality scoring.
6. Add the production CLI, batch conversion, and tagged release workflow.

See [architecture.md](docs/architecture.md) for the intended component boundaries.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please report security issues using the
process in [SECURITY.md](SECURITY.md), not through a public issue.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
