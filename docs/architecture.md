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
Preflight and safe format detection
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

- **Core API:** accepts a supported input and returns a structured conversion result.
- **Adapters:** understand one source format and emit common document blocks.
- **OCR:** processes only pages or embedded images selected by explicit quality rules.
- **Normalization:** performs fact-preserving structural cleanup on document blocks.
- **Quality:** records coverage and fidelity signals and chooses a result status.
- **Serializers:** create deterministic Markdown, JSON, and audit artifacts.

The library will not contain application-specific storage, authentication,
embedding, vector search, agent, or UI code. These belong to consumers such as
Kontakt AI.

## Version-one boundary

Version one will support DOCX, PDF, and XLSX conversion only. It will expose a
Python API and a CLI. Chunking, embeddings, and search-index writes are explicitly
outside version one.

