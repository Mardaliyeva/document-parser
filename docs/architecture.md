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
Format adapter (DOCX, PDF, or XLSX)
         |
         v
Document IR
         |
         +-- selective local OCR
         +-- native/OCR reconciliation
         +-- structural normalization
         +-- quality evaluation
         |
         v
Markdown + JSON + report + assets
```

## Component boundaries

- **Core API:** accepts a supported input and returns a validated `Document` IR.
- **Input preparation:** snapshots input without closing caller-owned streams, applies size
  limits, hashes bytes, and cleans temporary storage deterministically.
- **Detection:** recognizes PDF signatures and required DOCX/XLSX ZIP package parts without
  extracting the archive or trusting its extension.
- **Adapters:** understand one source format and emit common document blocks.
- **OCR:** processes only pages or embedded images selected by explicit quality rules.
- **Normalization:** performs fact-preserving structural cleanup on document blocks.
- **Quality:** records coverage and fidelity signals and chooses a result status.
- **Serializers:** create deterministic Markdown, JSON, and audit artifacts.

The library will not contain application-specific storage, authentication,
embedding, vector search, agent, or UI code. These belong to consumers such as
Kontakt AI.

## Core API contract

`inspect_source()` and `DocumentParser.inspect()` run input preparation and
preflight without requiring an adapter. `parse()` and `DocumentParser.parse()`
continue through the registry and return a validated `Document`.

Strings are paths, never raw text. Bytes and binary streams may receive a
filename for provenance, but detection is always based on content. Caller-owned
streams are not closed and their position is restored when they are seekable.

The adapter registry is immutable after `DocumentParser` construction. It allows
one adapter per `DocumentFormat`; duplicate registrations fail immediately.
Built-in adapters will be added lazily in later phases so importing the core does
not import OCR, Office, or PDF engines.

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

## Version-one boundary

Version one will support DOCX, PDF, and XLSX conversion only. It will expose a
Python API and a CLI. Chunking, embeddings, and search-index writes are explicitly
outside version one. The current `0.2.0a1` core recognizes those formats but does
not yet include their parsing adapters or Markdown serializers.
