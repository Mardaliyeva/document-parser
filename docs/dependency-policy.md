# Dependency and model policy

## Purpose

`document-parser` is intended to remain usable in open-source and proprietary
systems without strong-copyleft obligations. Every runtime dependency and model
asset must be reviewed before it is added.

## Normally allowed

- Apache-2.0
- MIT
- BSD-2-Clause
- BSD-3-Clause
- ISC
- Python-2.0 / PSF

Equivalent permissive licenses require an explicit review and an accompanying
record in the pull request that introduces them.

## Rejected by default

- AGPL
- GPL
- SSPL
- source-available licenses that are not open-source licenses
- dependencies or model assets with missing or ambiguous license terms

LGPL dependencies are not automatically accepted. They require a separate legal
and technical review of the exact version, linking model, and distribution path.

## Models

The license for a Python package does not automatically cover its downloaded
model weights. Each model must have an entry recording:

- model name and version;
- source URL;
- checksum;
- code license;
- weight/data license;
- required attribution;
- redistribution constraints.

Model binaries must not be committed to the Git repository. They will be fetched
and verified during a controlled build step once OCR support is introduced.

## Enforcement

Continuous integration installs the package without development extras in a
separate environment. The allowlist is enforced against that runtime environment,
so developer tooling does not hide or falsely block runtime changes. CI also
produces a runtime license report and reproducible CycloneDX SBOM. Introducing a
rejected, unknown, or unreviewed runtime license must fail the license job.

## Current runtime dependencies

The native-conversion release uses:

- Pydantic 2 for immutable IR and result validation;
- python-docx and lxml for DOCX plus bounded OOXML parsing;
- pypdf and pdfplumber for PDF validation, objects, text layout, and tables;
- openpyxl and defusedxml for safe XLSX workbook processing;
- Pillow for decoded image payload support required by the format adapters.

Their runtime dependency trees are checked in CI against the allowlist. PyMuPDF
is intentionally excluded because its AGPL/commercial licensing does not match
this project's permissive runtime policy. No OCR engine or model asset is a
runtime dependency in release `0.3.0a1`.
