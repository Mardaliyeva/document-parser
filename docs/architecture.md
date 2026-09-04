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
AdapterOutput(Document IR 0.1 + asset payloads)
         |
         v
Canonical Markdown serializer
         |
         v
ConversionResult(Document + Markdown + assets)
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
- **Normalization:** performs fact-preserving structural cleanup on document blocks.
- **Quality:** records coverage and fidelity signals and chooses a result status.
- **Serializer:** creates deterministic RAG-friendly Markdown from validated IR.

The library will not contain application-specific storage, authentication,
embedding, vector search, agent, or UI code. These belong to consumers such as
Kontakt AI.

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

## Document IR 0.1

The immutable Pydantic IR uses discriminated blocks for containers, headings,
paragraphs, lists, tables, figures, and page breaks. Nested blocks preserve list,
table-cell, section, page, and worksheet structure. Source locations can carry
page, worksheet, cell-range, asset, confidence, and coordinate provenance.

Every `Document` uses `sha256:<source hash>` as its identity. Block and asset IDs
must be unique, figures must reference declared assets, and table merges cannot
overlap or exceed declared dimensions. JSON serialization and validation use the
same schema version.

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

## Markdown contract

The in-house serializer has no runtime dependency. It emits document/sheet
titles, heading hierarchy, nested lists, GFM tables for simple grids, HTML tables
for merges or nested cells, content-addressed image links, and PDF page markers.
Repeated headers/footers and hidden sheets remain in IR but are omitted by
default. Fixed IR and options produce the same LF-normalized output with one
final newline.

## Version-one boundary

Version one will support DOCX, PDF, and XLSX conversion only. It will expose a
Python API and a CLI. Chunking, embeddings, and search-index writes are explicitly
outside version one. Release `0.4.0a1` includes selective local PDF OCR while
retaining IR schema `0.1`. Broader native/OCR reconciliation, structural
normalization, and cross-document quality scoring remain future work.
