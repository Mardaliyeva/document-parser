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

Model binaries must not be committed to the Git repository. They are fetched
only through the explicit `prepare_ocr_models()` API. The generated local
manifest records the release family, source URL, archive hash and size, maximum
size, engine compatibility, declared license/notice, and every extracted file's
size and SHA-256. `parse()` and `convert()` only verify that local inventory and
never download or update it.

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
this project's permissive runtime policy.

Release `0.6.0a1` keeps OCR outside the base dependency set. The `ocr` extra
adds PaddleOCR, pypdfium2, and platformdirs. PaddlePaddle is deliberately not
pinned by the package because its CPU/GPU wheel and installation index are
platform-specific; consumers must install a compatible 3.x runtime explicitly.
Base and OCR-extra environments receive separate license reports and CycloneDX
SBOMs. PaddleOCR model files are not distributed in the wheel or source archive.

The OCR-extra audit has one documented LGPL exception: PaddleOCR's current
`paddlex[ocr-core]` dependency installs `python-bidi`. It is an unmodified,
dynamically imported library used as a normal Python dependency; it is not part
of the base wheel and does not change this project's Apache-2.0 license. CI
allows LGPL metadata only in the isolated OCR-extra audit. A PaddleOCR upgrade
must re-check this dependency and its exact license terms.

Normalization, quality scoring, batch conversion, and the CLI use only the
Python standard library plus the existing validated models. No CLI framework or
text-similarity package is added to runtime dependencies. Release artifacts are
also installed into an isolated environment before their runtime SBOM is built.
