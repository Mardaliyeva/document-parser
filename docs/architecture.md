# Architecture

## Goal

The library will normalize format-specific extraction results into a common
document intermediate representation before producing Markdown. Markdown is a
serialization for people and retrieval systems; it is not the lossless source
of truth.

```text
Path / bytes / stream
         |
         v
Bounded seekable snapshot + SHA-256
         |
         v
Preflight and content-based format detection
         |
         v
Immutable adapter registry
         |
         v
Native adapter
  +-- DOCX: python-docx + bounded OOXML
  +-- PDF: pypdf + pdfplumber
  +-- XLSX: openpyxl + defusedxml
         |
         v
Selective PDF OCR (OFF / AUTO / FORCE)
  +-- bounded pypdfium2 page render
  +-- injected OcrEngine or lazy PaddleOCR
  +-- TEXT or STRUCTURED semantic results
  +-- native/OCR provenance and quality diagnostics
         |
         v
Native/OCR reconciliation
         |
         v
Fact-preserving structural normalization
         |
         v
Deterministic quality scoring
         |
         v
AdapterOutput(Document IR 0.2 + asset payloads)
         |
         v
Canonical Markdown serializer
         |
         v
ConversionResult(Document + Markdown + assets)
         |
         v
Python API, atomic CLI bundle, or optional local UI
```

OCR is an opt-in post-processing stage for PDF adapter output. The native PDF
adapter first marks conservative page candidates with `pdf.ocr_required`. In
`AUTO`, only those pages are rendered; in `FORCE`, every non-empty PDF page is
rendered; in `OFF`, neither the renderer nor OCR engine is imported. DOCX and
XLSX never enter this stage.

## Component boundaries

- **Core API:** exposes inspection, IR parsing, Markdown serialization, and bundled conversion.
- **Input preparation:** snapshots input without closing caller-owned streams, applies size
  limits, hashes bytes, and cleans temporary storage deterministically.
- **Detection:** recognizes PDF signatures and required DOCX/XLSX ZIP package parts without
  extracting the archive or trusting its extension.
- **Adapters:** understand one source format and emit `AdapterOutput` with common blocks and assets.
- **OCR:** renders only selected PDF pages and maps engine-neutral semantic results into IR.
- **Reconciliation:** compares overlapping native/OCR blocks and keeps explicit source decisions.
- **Normalization:** performs conservative cleanup while recording changed source values.
- **Quality:** scores coverage, confidence, structure, and fidelity and resolves review status.
- **Serializer:** creates deterministic RAG-friendly Markdown from validated IR.
- **Batch/CLI:** writes complete bundles atomically and isolates per-source failures.
- **Local UI:** maps one conversion bundle to Gradio views and short-lived downloads without
  duplicating parser, OCR, normalization, quality, or serialization logic.

The library will not contain application-specific storage, authentication,
embedding, vector search, hosted application, or agent code. The optional UI is
a localhost-only test surface; production UI and Kontakt AI integration remain
consumer responsibilities.

## Core API contract

`inspect_source()` and `DocumentParser.inspect()` run input preparation and
preflight. `parse()` and `DocumentParser.parse()` continue through the registry
and return a validated `Document`. `to_markdown()` serializes an existing IR,
while `convert()` and `DocumentParser.convert()` return `ConversionResult` with
the IR, Markdown, and asset payloads.

Strings are paths, never raw text. Bytes and binary streams may receive a
filename for provenance, but detection is always based on content. Caller-owned
streams are not closed and their position is restored when they are seekable.

The adapter registry is immutable after `DocumentParser` construction. It allows
one adapter per `DocumentFormat`; duplicate registrations fail immediately.
`adapters=None` installs the DOCX/PDF/XLSX built-ins, while an explicit empty or
custom iterable replaces them. Heavy Office and PDF modules remain lazy and are
not imported with the package root.

`DocumentParser` accepts an injected `OcrEngine`. This keeps the core independent
of PaddleOCR and allows deterministic unit tests or alternative local engines.
The built-in engine is constructed only when OCR is enabled and at least one
page was selected. Renderer and model/configuration failures use typed OCR
exceptions; an isolated page inference failure preserves the native page and
sets the document to `partial`.

## Document IR 0.2

The immutable Pydantic IR uses discriminated blocks for containers, headings,
paragraphs, lists, tables, figures, and page breaks. Nested blocks preserve list,
table-cell, section, page, and worksheet structure. Source locations can carry
page, worksheet, cell-range, asset, confidence, and coordinate provenance.

Every `Document` uses `sha256:<source hash>` as its identity. Block and asset IDs
must be unique, figures must reference declared assets, and table merges cannot
overlap or exceed declared dimensions. Schema `0.2` adds a `QualityReport`;
schema `0.1` payloads remain readable but cannot contain that field.

Expected failures use typed exceptions. A returned document can be `complete`,
`partial`, or `needs_review`; operations that fail do not return a synthetic
`failed` document.

## Native adapter behavior

The DOCX adapter uses `python-docx` for relationships and ordered document
objects. Bounded `lxml` reads cover numbering, revisions, and note stories. It
preserves headings, formatted spans, hyperlinks, nested lists, merge-aware
tables, sections, header/footer/note stories, page breaks, and embedded images.
External relationships are never fetched. Unsupported charts, SmartArt, and
equations create diagnostics and can make the document `partial`.

The PDF adapter uses `pypdf` for file validation, metadata, encryption checks,
and images, and `pdfplumber` for words, fonts, coordinates, layout, and tables.
Every page becomes a page container with point coordinates. Table words are not
duplicated as paragraphs, repeated margin text is marked, and conservative font
heuristics infer headings. Image-heavy pages with too little native text become
OCR candidates. A failed page is isolated so other pages remain available.

The XLSX adapter uses `openpyxl` to load formula and cached-value views of a
workbook; formulas are never evaluated. Formal tables are emitted first and
other non-empty cells become connected rectangular regions. Raw values, display
text, formulas, merges, hidden-state metadata, and images are preserved. Charts
and pivots are diagnosed as non-rendered content.

Each asset uses `asset:sha256:<digest>` identity and a safe content-addressed
filename. Manifest and payload identity, hash, MIME, and size are validated.
Input, archive, worksheet, and asset limits raise `UnsafeDocumentError` rather
than being downgraded to warnings.

## Selective OCR behavior

The simple local UI fixes AUTO/STRUCTURED AZ+EN+RU and CLEAN Markdown as its
conversion policy. Users upload and convert without configuration controls;
technical results are collapsed. This wrapper delegates to the same parser,
without changing Python/CLI OCR OFF defaults or the extraction engines.

`OcrPageInput` carries a bounded PNG rendering, PDF point dimensions, rotation,
and page identity. `OcrPageResult` contains ordered semantic regions, text-line
coordinates, table grids, confidence, model names, and engine diagnostics.
These contracts are frozen Pydantic models and contain no Paddle-specific
objects.

The built-in engine uses local PP-OCRv6 models for Azerbaijani/English text and
an East-Slavic PP-OCRv5 recognizer for Russian candidates. Script and confidence
rules reconcile the two recognition results deterministically. The structured
profile additionally uses PP-StructureV3 layout/table components; formula,
chart, seal, and cloud/VLM features are disabled.

OCR blocks receive point-space provenance and `extraction_method="ocr"`.
Replaced native blocks are recursively retained with
`active_for_rag=False`; the Markdown serializer skips them. If OCR finds no
text, native content remains active as a fallback. Status precedence is
`partial`, then `needs_review`, then `complete`.

Model preparation is the only network-enabled OCR operation. It uses HTTPS,
bounded downloads, safe tar validation, atomic directory replacement, and a
manifest containing archive and extracted-file hashes. Parsing verifies the
local model inventory and does not download or update models.

## Reconciliation, normalization, and quality

Reconciliation is page-local. A native/OCR pair must meet the configured
geometry threshold before text similarity is considered. Near-identical pairs
are duplicates; disagreements are retained as conflicts. Scan candidates may
prefer OCR when its confidence clears the configured margin, while forced OCR
on native pages prefers native extraction. Unmatched content stays active.

Normalization applies Unicode NFC, stable line endings, safe whitespace
cleanup, adjacent equal-format span merging, obvious heading-jump repair, and
RAG exclusion for empty or repeated-margin content. Original spans and heading
levels are recorded when changed. Body duplicates, hyphenation, formulas, table
geometry, coordinates, and assets are not semantically rewritten.

Quality uses a documented weighted score: 35% coverage, 30% confidence, 20%
structure, and 15% fidelity. Unit summaries are emitted per page, worksheet, or
whole document. Existing `partial` status cannot be downgraded. A low score,
unresolved conflict, or explicit OCR review signal produces `needs_review`.

## CLI bundle boundary

Directory discovery filters supported extensions, while routing remains
content-based. Workers own independent parser instances and reports are ordered
by normalized source path. Successful conversions are staged and renamed into
content-addressed bundles containing Markdown, IR JSON, a manifest, and assets.

## Markdown contract

The in-house serializer has no runtime dependency. It emits document/sheet
titles, heading hierarchy, nested lists, and GFM tables for simple grids. The
default CLEAN profile emits contextual records for complex tables, without
generated HTML, image links, or page comments. FIDELITY explicitly restores
HTML tables, underline, content-addressed image links, and page markers.
Repeated headers/footers and hidden sheets remain in IR but are omitted by
default. Fixed IR and options produce the same LF-normalized output with one
final newline.

## Optional local UI boundary

`document_parser.ui` is intentionally absent from the package root imports.
Gradio is loaded only by `build_ui()` or the `document-parser ui` command, so a
base import and every non-UI workflow remain independent of the optional UI
dependency. The controller calls the same `DocumentParser.convert()` and
`write_conversion_bundle()` functions as Python and CLI users.

One browser session owns at most one result job. Jobs are created beneath an
application-owned temporary root, never accept browser-provided output paths,
and contain only the generated bundle and deterministic ZIP. Session deletion,
clear, replacement, TTL cleanup, and server shutdown remove these artifacts.
The server address and sharing mode are fixed to `127.0.0.1` and `False`.
Every callback endpoint is private and the queue permits only one concurrent
conversion, preventing duplicate OCR model loads.

## Version-one boundary

Version one supports DOCX, PDF, and XLSX conversion through Python, CLI, and an
optional localhost-only inspection UI. Development release `0.8.0a1` uses IR
schema `0.2` and includes
selective OCR, reconciliation, normalization, quality scoring, and atomic batch
bundles. Chunking, embeddings, search-index writes, application storage, and
Kontakt-specific mapping remain outside this library.
