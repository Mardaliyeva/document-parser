# document-parser

[![CI](https://github.com/Mardaliyeva/document-parser/actions/workflows/ci.yml/badge.svg)](https://github.com/Mardaliyeva/document-parser/actions/workflows/ci.yml)
![Python 3.11–3.12](https://img.shields.io/badge/python-3.11%E2%80%933.12-blue)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

`document-parser` is a local-first Python library for converting documents into
structure-preserving data suitable for Markdown generation and downstream RAG
workflows.

> **Status:** pre-alpha. Native DOCX/PDF/XLSX conversion, opt-in local OCR,
> source reconciliation, conservative normalization, quality reporting, and a
> production-oriented batch CLI are available. OCR is disabled by default and
> never uses a cloud service. The current development package is `0.6.0a1`
> with Document IR schema `0.2`.

## Contents

- [Why document-parser?](#why-document-parser)
- [Installation](#installation)
- [Supported inputs](#supported-inputs)
- [Processing pipeline](#processing-pipeline)
- [Quick start](#quick-start)
- [Project principles](#project-principles)
- [Format behavior](#format-behavior)
- [Python API](#python-api)
- [Extending the parser](#extending-the-parser)
- [Configuration reference](#configuration-reference)
- [Optional local OCR](#optional-local-ocr)
- [Document IR, Markdown, and quality](#document-ir-markdown-and-quality)
- [Command-line and batch conversion](#command-line-and-batch-conversion)
- [Errors and diagnostics](#errors-and-diagnostics)
- [Security model](#security-model)
- [Known limitations](#known-limitations)
- [Development and testing](#development-and-testing)

## Why document-parser?

Document extraction is more than calling a text-extraction function. A useful
RAG input must preserve headings, lists, tables, worksheets, page locations,
images, formulas, and extraction provenance without inventing or silently
rewriting facts. This project therefore produces two representations:

- an immutable, loss-preserving JSON Document IR for machines;
- deterministic, RAG-friendly Markdown for indexing and human inspection.

The parser is deliberately independent of chunking, embeddings, vector
databases, application storage, and Kontakt-specific index fields. Consumers
can build those layers on top of the stable IR or Markdown output.

## Installation

### Requirements

- CPython 3.11 or 3.12;
- Windows, Linux, or macOS;
- no cloud account or API key for native DOCX/PDF/XLSX conversion.

The project is currently a development release and is not assumed to be
published on PyPI. Install it from a clone:

```bash
git clone https://github.com/Mardaliyeva/document-parser.git
cd document-parser
python -m venv .venv
```

Alternatively, install the current Git revision directly:

```bash
python -m pip install "document-parser @ git+https://github.com/Mardaliyeva/document-parser.git"
```

Windows PowerShell:

```powershell
./.venv/Scripts/python -m pip install --upgrade pip
./.venv/Scripts/python -m pip install .
```

Linux or macOS:

```bash
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install .
```

For an editable development installation:

```bash
python -m pip install -e ".[dev]"
```

For OCR support, install the optional integration and then install a compatible
PaddlePaddle 3.x runtime separately:

```bash
python -m pip install -e ".[ocr]"
```

PaddlePaddle uses platform-specific CPU/GPU packages, so follow its
[official installation guide](https://www.paddleocr.ai/latest/en/version3.x/paddlepaddle_installation.html).
Installing `document-parser[ocr]` installs PaddleOCR and PDF rendering support,
but intentionally does not select a PaddlePaddle runtime on your behalf.

## Supported inputs

| Input | Main extraction path | Preserved information |
| --- | --- | --- |
| DOCX | `python-docx` plus bounded OOXML reads | Heading hierarchy, styled spans, hyperlinks, nested lists, tables and merges, sections, headers, footers, notes, page breaks, metadata, and images |
| Born-digital PDF | `pypdf` and `pdfplumber` | Pages, native text, coordinates, reading order, headings, repeated margins, tables, metadata, and images |
| Scanned or mixed PDF | Native PDF adapter plus optional `pypdfium2` and PaddleOCR | Selective page OCR, orientation, layout, reading order, AZ/EN/RU text, confidence, coordinates, lists, tables, figures, and native/OCR provenance |
| XLSX | `openpyxl` plus `defusedxml` protection | Worksheets, rectangular regions, formal tables, formulas, cached/displayed values, typed values, merges, hidden-state metadata, and images |

Legacy Office files, macro-enabled packages, encrypted Office documents, and
encrypted PDFs are rejected. Excel formulas are preserved but never evaluated.
External relationships and images are never fetched from the internet.

The accepted top-level extensions are `.docx`, `.pdf`, and `.xlsx`, but
routing is based on file content. A renamed PDF is still recognized as a PDF;
the extension mismatch becomes a diagnostic or, in strict mode, an error.

The following are intentionally unsupported:

- `.doc`, `.xls`, `.docm`, `.xlsm`, PPT/PPTX, CSV, and standalone images;
- password-protected or encrypted input;
- cloud OCR, macros, formula execution, and remote asset fetching;
- chunking, embeddings, vector databases, retrieval, and application storage.

## Processing pipeline

```text
path / bytes / bytearray / binary stream
                    |
                    v
bounded snapshot + SHA-256 + content detection
                    |
                    v
         DOCX / PDF / XLSX adapter
                    |
                    v
     selective PDF OCR (off/auto/force)
                    |
                    v
       native/OCR reconciliation
                    |
                    v
 fact-preserving normalization + quality score
                    |
                    v
       immutable Document IR schema 0.2
                    |
                    v
        canonical Markdown + assets
```

The JSON IR is the source of truth. Markdown is a deterministic serialization
optimized for inspection and downstream retrieval pipelines.

## Quick start

Convert one document and write a complete bundle:

```python
from document_parser import convert, write_conversion_bundle

result = convert("documents/report.docx")
bundle = write_conversion_bundle(result, "converted")

print(result.document.status.value)
print(result.document.quality.overall_score)
print(bundle)
```

The resulting directory contains:

```text
converted/
└── report-<sha256-prefix>/
    ├── document.md
    ├── document.json
    ├── manifest.json
    └── assets/
```

## Project principles

- Local processing by default, with no document telemetry.
- Structure and factual fidelity before cosmetic cleanup.
- A lossless JSON intermediate representation as the source of truth.
- Deterministic outputs for fixed input, options, and model versions.
- Selective OCR instead of OCRing every page or embedded image.
- Permissively licensed dependencies and model assets only.

## Format behavior

### DOCX

The DOCX adapter uses `python-docx` for the object model and relationships,
then performs bounded OOXML reads for structures that are not exposed cleanly
by the high-level API.

- Word heading styles and outline levels become `HeadingBlock` values.
- Runs become ordered `TextSpan` values with bold, italic, underline,
  strikethrough, code, and hyperlink information.
- Paragraphs and tables retain their source order.
- Word numbering definitions become ordered/unordered nested lists.
- Horizontal and vertical merges become row/column spans.
- Inline and floating images become content-addressed figures/assets.
- Sections, manual page breaks, headers, footers, footnotes, and endnotes retain
  story/location attributes.
- Inserted revision text is kept; deleted revision text is excluded and
  diagnosed.
- Charts, SmartArt, equations, and unresolved external content are not fetched
  or rendered and may produce a `partial` result.

### Born-digital PDF

The native PDF adapter uses `pypdf` for validation, metadata, pages, encryption,
and image objects, and `pdfplumber` for characters, words, fonts, coordinates,
layout, and table detection.

- Every page becomes a `ContainerBlock(role="page")`.
- Words are grouped into lines, paragraphs, and a coordinate-based reading
  order.
- Font size and layout provide conservative heading candidates.
- Tables are detected using drawn lines first and text alignment second.
- Words assigned to a table are not emitted a second time as paragraphs.
- Repeated top/bottom lines are marked as header/footer content.
- Bounding boxes use PDF point coordinates with canvas dimensions.
- One failed page can be isolated while other pages remain available.

A page becomes an OCR candidate when it contains fewer than the configured
native alphanumeric threshold and images cover at least the configured fraction
of the page. A truly blank page is not OCRed.

### XLSX

The XLSX adapter opens the workbook in formula and cached-value modes.

- Excel formulas are stored as text and never executed.
- Each worksheet becomes a `ContainerBlock(role="sheet")`.
- Formal Excel tables are emitted before other cell regions.
- Remaining non-empty cells are grouped into regions separated by completely
  blank rows/columns.
- Raw values, displayed text, cached values, formulas, number formats, dates,
  times, booleans, numeric values, and errors remain distinguishable.
- Merged ranges become table spans.
- Hidden sheet/row/column metadata is retained according to options.
- Embedded worksheet images become assets.
- Charts and pivot content produce diagnostics rather than invented text.

## Python API

### `convert()`: IR, Markdown, and assets

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

The asset filename and SHA-256 are validated against `Document.assets`. Use
`write_conversion_bundle()` when the standard atomic directory layout is
preferred over manually writing the returned values.

### `parse()` and `to_markdown()`

Use `parse()` when only the loss-preserving IR is needed and `to_markdown()` when
an existing `Document` should be serialized separately:

```python
from document_parser import FormulaMode, MarkdownOptions, TableMode, parse, to_markdown

document = parse(pdf_bytes, filename="report.pdf")
markdown = to_markdown(
    document,
    options=MarkdownOptions(
        include_source_markers=True,
        formula_mode=FormulaMode.BOTH,
        table_mode=TableMode.AUTO,
    ),
)
```

### `inspect_source()`

Validate, hash, and content-detect an input without running an adapter:

```python
from document_parser import inspect_source

source = inspect_source("documents/report.pdf")
print(source.format.value)
print(source.media_type)
print(source.sha256)
print(source.extension_matches)
```

### Accepted Python inputs

| Value passed as `source` | Behavior |
| --- | --- |
| `str` | Always treated as a filesystem path, never as raw text |
| `os.PathLike` | Read as a filesystem path |
| `bytes` / `bytearray` | Parsed as document bytes; pass `filename=` for provenance |
| binary stream | Read without closing it; a seekable stream's cursor is restored |

`filename` must be a basename such as `report.pdf`, not a path. Format
detection always uses content rather than trusting this name.

### Reusable `DocumentParser`

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

`DocumentParser.supported_formats` reports the active immutable registry.
Reuse a parser when the same options and OCR engine should be applied to
multiple calls. Do not register the same `DocumentFormat` twice.

### JSON round-trip

Every public model is a frozen Pydantic v2 model with unknown fields rejected:

```python
from document_parser import Document

payload = document.model_dump_json(indent=2)
restored = Document.model_validate_json(payload)
assert restored == document
```

Normalization and scoring can also be applied explicitly to an existing IR:

```python
from document_parser import assess_quality, normalize_document

normalized = normalize_document(restored)
quality = assess_quality(normalized)
print(quality.overall_score, quality.flags)
```

Input, archive, worksheet, and asset limits are enforced before or during
extraction. Recoverable fidelity losses produce diagnostics and `partial`; an
OCR candidate produces `needs_review` while preserving available native content.

After extraction, the parser reconciles overlapping native/OCR sources,
normalizes text without semantic rewriting, and attaches a transparent
`QualityReport` to IR schema `0.2`. Legacy IR `0.1` JSON remains readable.

```python
from document_parser import NormalizationOptions, ParseOptions, QualityOptions

options = ParseOptions(
    normalization=NormalizationOptions(repair_heading_levels=True),
    quality=QualityOptions(review_threshold=0.75),
)
```

The dependency-free Markdown serializer preserves hierarchy, emits simple
tables as GFM and merge-aware tables as HTML, uses content-addressed asset links,
hides repeated headers/footers and hidden sheets by default, and marks PDF pages
with `<!-- page: N -->`. Metadata, coordinates, raw spreadsheet values, formulas,
diagnostics, and hidden IR content remain available on `result.document`.

### Python batch conversion

```python
from document_parser import BatchOptions, convert_batch

report = convert_batch(
    ["documents", "extra/report.pdf"],
    "converted",
    batch_options=BatchOptions(
        recursive=True,
        jobs=4,
        overwrite=False,
        fail_fast=False,
    ),
)

for item in report.items:
    print(item.source, item.status.value, item.output_directory, item.error_code)
```

One failed document does not stop the remaining batch unless `fail_fast=True`.
Reports and discovered paths are sorted deterministically.

## Extending the parser

### Custom format adapter

A custom adapter can replace a built-in implementation for one of the supported
`DocumentFormat` values:

```python
from document_parser import (
    AdapterInput,
    AdapterOutput,
    DocumentFormat,
    DocumentParser,
    ParseOptions,
)


class MyPdfAdapter:
    format = DocumentFormat.PDF

    def parse(
        self,
        source: AdapterInput,
        options: ParseOptions,
    ) -> AdapterOutput:
        # Build a validated Document plus matching AssetPayload values.
        ...


parser = DocumentParser(adapters=[MyPdfAdapter()])
```

An explicit custom adapter iterable replaces the full built-in registry; it
does not merge with it. `adapters=[]` is useful for inspection-only or
registry-error tests. Adapter output must refer to the exact prepared
`SourceInfo`, and every asset manifest entry must have one matching payload.
Unexpected implementation errors are wrapped as `AdapterExecutionError`.

Custom OCR engines are covered in [Optional local OCR](#custom-ocr-engine).

## Configuration reference

All option objects are immutable. Construct a `ParseOptions` tree once and pass
it to `parse()`, `convert()`, `DocumentParser`, or `convert_batch()`.

### Safety and resource limits

| `ParseOptions` field | Default | Purpose |
| --- | ---: | --- |
| `max_input_bytes` | 100 MiB | Maximum source size |
| `spool_threshold_bytes` | 8 MiB | Memory-to-temporary-file threshold |
| `max_archive_entries` | 10,000 | Maximum DOCX/XLSX ZIP entries |
| `max_archive_uncompressed_bytes` | 1 GiB | Maximum total expanded ZIP size |
| `max_archive_compression_ratio` | 100 | ZIP-bomb ratio limit |
| `max_assets` | 2,000 | Maximum extracted asset count |
| `max_asset_bytes` | 25 MiB | Maximum size of one asset |
| `max_total_asset_bytes` | 100 MiB | Maximum combined asset payload |
| `strict_extension` | `False` | Turn an extension/content mismatch into an error |

Example:

```python
from document_parser import ParseOptions

options = ParseOptions(
    max_input_bytes=50 * 1024 * 1024,
    max_assets=500,
    strict_extension=True,
)
```

### DOCX options

| `DocxOptions` field | Default | Effect |
| --- | ---: | --- |
| `include_headers` | `True` | Preserve header stories in IR |
| `include_footers` | `True` | Preserve footer stories in IR |
| `include_footnotes` | `True` | Preserve footnote stories |
| `include_endnotes` | `True` | Preserve endnote stories |
| `include_hidden_text` | `False` | Include text marked hidden in Word |

Header/footer content can remain in IR while the default Markdown serializer
omits it to avoid repeated RAG text.

### Native PDF options

| `PdfOptions` field | Default | Effect |
| --- | ---: | --- |
| `detect_tables` | `True` | Enable line/text-alignment table heuristics |
| `infer_headings` | `True` | Infer headings from font/layout information |
| `min_native_alphanumeric_chars` | `20` | Native-text threshold used by scan detection |
| `scan_image_coverage_threshold` | `0.50` | Minimum image coverage for a scan candidate |
| `repeated_margin_min_fraction` | `0.60` | Repetition threshold for headers/footers |

### XLSX options

| `XlsxOptions` field | Default | Effect |
| --- | ---: | --- |
| `include_hidden_sheets` | `True` | Preserve hidden worksheets in IR |
| `include_hidden_rows` | `True` | Preserve hidden-row content and metadata |
| `include_hidden_columns` | `True` | Preserve hidden-column content and metadata |
| `max_worksheet_cells` | 1,000,000 | Reject oversized worksheet areas |

### Normalization options

| `NormalizationOptions` field | Default |
| --- | ---: |
| `enabled` | `True` |
| `reconcile_ocr` | `True` |
| `normalize_unicode` | `True` |
| `normalize_whitespace` | `True` |
| `repair_heading_levels` | `True` |
| `hide_repeated_margins` | `True` |
| `duplicate_text_similarity` | `0.96` |
| `geometry_overlap` | `0.50` |
| `ocr_confidence_margin` | `0.05` |

Normalization is conservative: it does not paraphrase, calculate formulas,
perform semantic correction, remove body duplicates, or change table geometry.
When text or a heading level changes, provenance is retained in block
attributes.

### Quality options

| `QualityOptions` field | Default |
| --- | ---: |
| `enabled` | `True` |
| `review_threshold` | `0.75` |
| `coverage_weight` | `0.35` |
| `confidence_weight` | `0.30` |
| `structure_weight` | `0.20` |
| `fidelity_weight` | `0.15` |

Weights must sum to `1.0`. The default score is:

```text
overall = 0.35 × coverage
        + 0.30 × confidence
        + 0.20 × structure
        + 0.15 × fidelity
```

### Markdown options

| `MarkdownOptions` field | Default | Effect |
| --- | ---: | --- |
| `include_document_title` | `True` | Emit metadata title |
| `include_source_markers` | `True` | Emit PDF page markers |
| `include_headers_footers` | `False` | Include Word/PDF repeated margins |
| `include_hidden_sheets` | `False` | Serialize hidden worksheets |
| `formula_mode` | `displayed` | `displayed`, `formula`, or `both` |
| `table_mode` | `auto` | `auto`, `gfm`, or `html` |
| `asset_prefix` | `assets/` | Prefix used by Markdown image links |

In `auto` mode, simple rectangular tables use GFM pipe syntax; merged or
nested tables use HTML so row/column spans are not lost.

## Optional local OCR

OCR is local, optional, and applies only to PDF pages. The base package neither
imports PaddleOCR nor looks for models while OCR is off.

### Prepare and verify models

Model downloads are an explicit administrative operation. They never happen
inside `parse()` or `convert()`.

CLI:

```powershell
document-parser models prepare --target ./models --profiles text --languages az,en,ru
document-parser models verify --target ./models
```

Use `--profiles structured,text` to prepare both profiles. Structured models
require more download size, disk space, memory, and startup time.

Python:

```python
from document_parser import OcrProfile, prepare_ocr_models, verify_ocr_models

report = prepare_ocr_models(
    "./models",
    profiles=(OcrProfile.STRUCTURED, OcrProfile.TEXT),
    languages=("az", "en", "ru"),
)
assert report.valid
assert verify_ocr_models("./models").valid
```

Preparation uses HTTPS, bounded downloads, safe archive extraction, SHA-256
inventories, and atomic replacement. Parsing accepts only a complete model
store whose `document-parser-models.json` manifest still matches every file.

### Enable OCR for conversion

After model preparation, document conversion is local and offline:

```python
from pathlib import Path

from document_parser import OcrMode, OcrOptions, OcrProfile, ParseOptions, convert

options = ParseOptions(
    ocr=OcrOptions(
        mode=OcrMode.AUTO,
        profile=OcrProfile.STRUCTURED,
        model_store=Path("models"),
        languages=("az", "en", "ru"),
        device="cpu",
        dpi=300,
    )
)
result = convert("mixed-or-scanned.pdf", options=options)
```

### Modes and profiles

| Setting | Value | Behavior |
| --- | --- | --- |
| mode | `off` | Default; do not import the renderer or OCR engine |
| mode | `auto` | OCR only native-PDF pages marked as scan candidates |
| mode | `force` | OCR every non-empty PDF page |
| profile | `structured` | Layout, titles, reading order, figures, and table structure |
| profile | `text` | Faster text detection/recognition; table structure is not guaranteed |

Default OCR limits and quality thresholds:

| `OcrOptions` field | Default |
| --- | ---: |
| `languages` | `("az", "en", "ru")` |
| `device` | `cpu` |
| `dpi` | `300` |
| `max_pages` | `200` |
| `max_page_pixels` | `40,000,000` |
| `max_total_pixels` | `500,000,000` |
| `min_region_confidence` | `0.50` |
| `min_page_confidence` | `0.75` |
| `max_low_confidence_fraction` | `0.20` |
| `use_orientation` | `True` |
| `use_unwarping` | `True` |

The built-in language layer uses PP-OCRv6 for Azerbaijani/English and an
East-Slavic PP-OCRv5 recognizer for Russian. OCR shadow/native blocks remain in
IR for provenance, but reconciliation marks the selected source as active for
RAG. A no-text OCR result keeps native content active as a safe fallback.

### Custom OCR engine

A consumer can inject another local or deterministic test engine without
installing PaddleOCR:

```python
from document_parser import DocumentParser, OcrMode, OcrOptions, ParseOptions

parser = DocumentParser(
    options=ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
    ocr_engine=my_engine,
)
result = parser.convert("scan.pdf")
```

`my_engine` must implement
`recognize(OcrPageInput, OcrOptions) -> OcrPageResult` and expose a `name`.

See [OCR setup and behavior](docs/ocr.md) for model-store security, custom
engines, limits, diagnostics, and reproducibility details.

## Document IR, Markdown, and quality

### Document IR 0.2

`Document` contains:

- `document_id`: deterministic `sha256:<source digest>`;
- `source`: detected format, media type, name, size, SHA-256, and preflight
  diagnostics;
- `metadata`: normalized title, authors, subject, keywords, language, and
  timezone-aware timestamps;
- `blocks`: ordered format-independent content;
- `assets`: validated, content-addressed asset manifests;
- `diagnostics`: structured, non-fatal extraction information;
- `status`: `complete`, `needs_review`, or `partial`;
- `quality`: overall and component quality measurements.

The discriminated block union includes:

- `ContainerBlock` for pages, sections, and sheets;
- `HeadingBlock` and `ParagraphBlock` with formatted `TextSpan` values;
- nested `ListBlock` structures;
- `TableBlock` with raw/displayed/formula values and merge spans;
- `FigureBlock` with an `AssetRef`;
- `PageBreakBlock`.

`SourceLocation` can retain page number, worksheet, A1 cell range, block
index, asset ID, confidence, and pixel/point bounding boxes. Block IDs and asset
IDs are deterministic and validated for uniqueness.

### Status semantics

| Status | Meaning |
| --- | --- |
| `complete` | Conversion completed without a required manual review signal |
| `needs_review` | Output exists, but scan detection, low confidence, conflict, or quality policy requires review |
| `partial` | Output exists, but one or more source elements/pages could not be fully represented |

Status priority is `partial > needs_review > complete`. Expected fatal
failures raise typed exceptions instead of returning a synthetic failed
`Document`.

### Canonical Markdown

The serializer:

- emits LF line endings and exactly one final newline;
- creates PDF page markers such as `<!-- page: 2 -->`;
- maps sheet containers to `## Sheet: <name>`;
- preserves nested lists with deterministic indentation;
- uses GFM or merge-aware HTML tables;
- escapes Markdown control characters and cell newlines;
- links assets through content-addressed filenames;
- excludes `active_for_rag=False` blocks;
- hides repeated headers/footers and hidden sheets by default;
- does not inject YAML front matter or quality metadata into the text.

Metadata, raw formulas, coordinates, inactive OCR/native alternatives, and
diagnostics remain available in `document.json` even when omitted from
Markdown.

## Command-line and batch conversion

The base package installs a dependency-free `document-parser` command. Run it
inside the activated virtual environment, or address the executable
directly as `./.venv/Scripts/document-parser.exe` on Windows and
`./.venv/bin/document-parser` on Linux/macOS.

```text
document-parser convert <file-or-directory>... --output <directory>
document-parser inspect <file>
document-parser models prepare [--target <directory>]
document-parser models verify [--target <directory>]
document-parser version
```

### Convert examples

```powershell
# One file
document-parser convert ./documents/report.docx --output ./converted

# All supported files in a directory tree, using four workers
document-parser convert ./documents --recursive --jobs 4 --output ./converted

# Selective structured OCR
document-parser convert ./documents/scans --recursive --output ./converted `
  --ocr auto --ocr-profile structured `
  --languages az,en,ru --model-store ./models

# Replace matching content-addressed bundles and fail CI on review states
document-parser convert ./documents --output ./converted `
  --overwrite --fail-on-review
```

### Convert flags

| Flag | Default | Meaning |
| --- | ---: | --- |
| `--output DIRECTORY` | required | Bundle output root |
| `--recursive` | off | Include nested input directories |
| `--jobs N` | `1` | Parallel workers, from 1 to 32 |
| `--overwrite` | off | Atomically replace an existing matching bundle |
| `--fail-fast` | off | Sequentially stop after the first failed item |
| `--fail-on-review` | off | Return exit code 1 for `needs_review` or `partial` |
| `--ocr` | `off` | `off`, `auto`, or `force` |
| `--ocr-profile` | `structured` | `structured` or `text` |
| `--languages` | `az,en,ru` | Comma-separated OCR language set |
| `--model-store` | platform default | Verified local OCR model directory |

More workers can multiply OCR model memory usage. `--fail-fast` intentionally
uses a sequential loop.

### Inspect without conversion

```powershell
document-parser inspect ./documents/report.pdf
```

This prints content-derived `SourceInfo` JSON, including the hash, detected
format, media type, supplied extension, and extension diagnostics.

### Bundle format

Each successful source is written atomically:

```text
<output>/
├── <safe-stem>-<source-sha256-prefix>/
│   ├── document.md
│   ├── document.json
│   ├── manifest.json
│   └── assets/
└── batch-report.json
```

`document.json` is the loss-preserving IR. `manifest.json` summarizes the
source, status, quality, diagnostics, and assets. `batch-report.json` contains
one deterministic entry for every attempted input, including isolated errors.
Existing bundles require `--overwrite`.

### Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Every selected input produced a bundle |
| `1` | A conversion/write failed, or `--fail-on-review` rejected a result |
| `2` | Invalid CLI arguments or configuration |
| `3` | Unsafe source/archive/output behavior was detected |

See [CLI and bundle format](docs/cli.md) for additional operational details.

## Errors and diagnostics

Catch `DocumentParserError` for expected library failures and use its stable
`code` value in application logic:

```python
from document_parser import DocumentParserError, convert

try:
    result = convert("incoming/document.pdf")
except DocumentParserError as exc:
    print(exc.code.value)
    print(exc.source_name)
    print(str(exc))
```

| Exception | Error code |
| --- | --- |
| `SourceNotFoundError` | `source_not_found` |
| `SourceReadError` | `source_read_error` |
| `SourceTooLargeError` | `source_too_large` |
| `UnsupportedFormatError` | `unsupported_format` |
| `InvalidDocumentError` | `invalid_document` |
| `UnsafeDocumentError` | `unsafe_document` |
| `AdapterNotAvailableError` | `adapter_not_available` |
| `AdapterExecutionError` | `adapter_execution_error` |
| `OcrDependencyNotAvailableError` | `ocr_dependency_not_available` |
| `OcrModelNotAvailableError` | `ocr_model_not_available` |
| `OcrConfigurationError` | `ocr_configuration_error` |
| `OcrExecutionError` | `ocr_execution_error` |

Non-fatal events are represented by structured `Diagnostic` objects with a
code, severity, message, optional source location, and JSON-compatible details.
Typical codes include `pdf.ocr_required`, `ocr.applied`,
`ocr.low_confidence`, `ocr.page_failed`, and
`reconciliation.conflict`.

## Security model

The parser treats all inputs as untrusted:

- format routing uses signatures and required package members, not extensions;
- ZIP central directories are checked without extracting the whole package;
- absolute paths, drive paths, `..` traversal, duplicate critical entries,
  encrypted entries, and suspicious compression ratios are rejected;
- legacy OLE and macro-enabled Office formats are rejected;
- formulas and macros are never evaluated;
- external relationships and assets are never downloaded;
- PDF OCR renders only bounded selected pages;
- OCR parsing never downloads or updates a model;
- caller streams are not closed, and parser-owned temporary files are cleaned;
- assets use SHA-256 identities and safe content-addressed filenames;
- CLI output rejects symlink components, traversal, and directory escape;
- bundle writes use same-filesystem staging and rollback-aware replacement.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and
[dependency-policy.md](docs/dependency-policy.md) for dependency/license rules.

## Determinism and reproducibility

For the same source bytes and options, native parsing produces stable source
hashes, IDs, block order, diagnostics, JSON, Markdown, and asset names. Batch
reports exclude timing data and remain sorted.

OCR mapping is deterministic for a fixed `OcrPageResult`. Byte-identical ML
output is expected only with the same model files, Paddle/PaddleOCR versions,
device, and runtime backend; CPU and GPU inference are not promised to match
byte for byte.

## Known limitations

This is a pre-alpha release. Important current boundaries are:

- native PDF layout and heading detection are heuristic;
- two-column native PDF prose can occasionally trigger the text-alignment table
  fallback;
- adjacent DOCX spans with different formatting can currently lose a
  significant boundary space in Markdown;
- a DOCX metadata title and an equivalent visible Title paragraph can both be
  serialized;
- the `TEXT` OCR profile does not guarantee table reconstruction;
- unsupported DOCX charts, SmartArt, and equations can make a result
  `partial`;
- XLSX charts and pivots are diagnosed but not rendered as table content;
- OCR applies to PDF pages only, not standalone image files;
- encrypted input and password callbacks are not supported;
- chunking and retrieval integration belong in the consuming application.

The two-column PDF case and the two DOCX serialization cases have explicit
`xfail` regression tests in the synthetic corpus, so they remain visible
until fixed.

## Development and testing

Create a Python 3.11 or 3.12 virtual environment and install the development
dependencies:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

On Linux or macOS, activate the equivalent virtual environment and use its
`python` executable.

Run the complete local quality gate:

```powershell
./.venv/Scripts/python -m ruff format --check .
./.venv/Scripts/python -m ruff check .
./.venv/Scripts/python -m mypy src tests
./.venv/Scripts/python -m pytest
./.venv/Scripts/python -m build
./.venv/Scripts/python -m twine check dist/*
```

CI tests Python 3.11/3.12 on Windows and Linux, verifies minimum dependency
versions, enforces 100% branch coverage and the runtime-license allowlist, and
creates a reproducible CycloneDX SBOM. A separate manual/weekly workflow runs
real PaddleOCR language and table smoke tests with a verified model cache.

The normal test suite uses an injected deterministic OCR engine and does not
download models. To opt into real OCR tests:

```powershell
$env:DOCUMENT_PARSER_RUN_OCR_INTEGRATION = "1"
$env:DOCUMENT_PARSER_OCR_MODEL_STORE = (Resolve-Path "./models")
./.venv/Scripts/python -m pytest -m ocr_integration
```

Deterministic DOCX, native/scanned/mixed/rotated/encrypted PDF, and XLSX
fixtures live in [tests/fixtures/synthetic](tests/fixtures/synthetic). Their
sizes and SHA-256 hashes are recorded in the corpus manifest.

## Roadmap

1. **Complete:** package, policy, CI, and release scaffold.
2. **Complete:** Document IR, safe input preparation, detection, and routing.
3. **Complete:** native DOCX/PDF/XLSX adapters and canonical Markdown.
4. **Complete:** opt-in selective local OCR for scanned and mixed PDF pages.
5. **Complete:** native/OCR reconciliation, structural normalization, and quality scoring.
6. **Complete:** production CLI, batch conversion, and tagged release workflow.

Chunking, embeddings, vector stores, and Kontakt-specific mapping remain
consumer responsibilities.

See [architecture.md](docs/architecture.md) for the intended component boundaries.

## Documentation

- [Architecture and component boundaries](docs/architecture.md)
- [OCR installation, models, and reproducibility](docs/ocr.md)
- [CLI commands, bundles, and exit codes](docs/cli.md)
- [Dependency and license policy](docs/dependency-policy.md)
- [Synthetic integration corpus](tests/fixtures/synthetic/README.md)
- [Changelog](CHANGELOG.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please report security issues using the
process in [SECURITY.md](SECURITY.md), not through a public issue.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
