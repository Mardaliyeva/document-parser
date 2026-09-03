# document-parser

`document-parser` is a local-first Python library for converting documents into
structure-preserving data suitable for Markdown generation and downstream RAG
workflows.

> **Status:** pre-alpha core. Input inspection and the Document IR are available;
> built-in format adapters and Markdown conversion are not implemented yet.

## Planned scope

- DOCX through native Office Open XML parsing.
- Born-digital PDF through native text and layout analysis.
- Scanned and mixed PDF through selective, local OCR.
- XLSX through native workbook, worksheet, cell, table, and formula parsing.
- Canonical Markdown, structured JSON, assets, and quality reports.

The project will not execute Office macros, evaluate Excel formulas, use cloud
OCR, or include embedding and vector-database integrations.

## Project principles

- Local processing by default, with no document telemetry.
- Structure and factual fidelity before cosmetic cleanup.
- A lossless JSON intermediate representation as the source of truth.
- Deterministic outputs for fixed input, options, and model versions.
- Selective OCR instead of OCRing every page or embedded image.
- Permissively licensed dependencies and model assets only.

## Core API

The current release can safely inspect paths, bytes, byte arrays, and binary
streams. Detection uses file content rather than trusting the extension:

```python
from document_parser import DocumentFormat, inspect_source

source = inspect_source(b"%PDF-1.7\n%%EOF\n", filename="example.pdf")
assert source.format is DocumentFormat.PDF
```

`DocumentParser.parse()` returns the validated, format-independent `Document`
IR when a matching adapter is registered. Version `0.2.0a1` intentionally ships
without built-in adapters, so parsing a recognized DOCX, PDF, or XLSX with the
default parser raises `AdapterNotAvailableError`.

```python
from document_parser import AdapterNotAvailableError, DocumentParser

parser = DocumentParser()

try:
    parser.parse(b"%PDF-1.7\n%%EOF\n", filename="example.pdf")
except AdapterNotAvailableError:
    pass
```

Advanced consumers may inject an implementation of `DocumentAdapter`. The
adapter receives a bounded, seekable `AdapterInput` and must return a `Document`
whose source matches `AdapterInput.info`. See
[architecture.md](docs/architecture.md) for the complete boundary.

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

CI additionally installs the package without development extras in an isolated
environment, enforces the runtime-license allowlist, and creates a reproducible
CycloneDX SBOM. Generated audit reports and model artifacts are not committed.

## Roadmap

1. **Complete:** define the public API, Document IR, safe input validation, and routing.
2. Add native DOCX conversion.
3. Add born-digital PDF conversion.
4. Add local OCR for scanned and mixed PDFs.
5. Add native XLSX conversion.
6. Add selective OCR for text-bearing Office images.
7. Add structural normalization, quality scoring, and canonical serializers.
8. Add the production CLI, batch processing, and tagged release workflow.

See [architecture.md](docs/architecture.md) for the intended component boundaries.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please report security issues using the
process in [SECURITY.md](SECURITY.md), not through a public issue.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
