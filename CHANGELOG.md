# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned

- Add native/OCR reconciliation and quality scoring.

## [0.4.0a1] - 2026-09-04

### Added

- Immutable engine-neutral OCR options, page, region, line, and table contracts.
- `OFF`, `AUTO`, and `FORCE` PDF OCR modes with structured and text profiles.
- Lazy local PaddleOCR bridge with Azerbaijani/English and Russian reconciliation.
- Bounded pypdfium2 rendering and pixel-to-PDF-point coordinate mapping.
- Explicit `prepare_ocr_models()` and offline `verify_ocr_models()` APIs.
- Safe model archive extraction, atomic model staging, hash inventories, and license metadata.
- OCR-specific typed exceptions, diagnostics, confidence review rules, and page-failure isolation.
- Custom `OcrEngine` injection for alternative engines and model-free testing.

### Changed

- Markdown now excludes inactive native shadow blocks after successful OCR.
- Scanned-page background figures are retained in IR but hidden from RAG output after OCR.
- OCR remains optional and disabled by default; DOCX/XLSX behavior and IR schema `0.1` are unchanged.
- Bumped the package version to `0.4.0a1`.

## [0.3.0a1] - 2026-09-03

### Added

- Built-in, lazily imported DOCX, born-digital PDF, and XLSX adapters.
- Immutable `AssetPayload`, `AdapterOutput`, and `ConversionResult` bundles.
- Public `convert()` and deterministic `to_markdown()` APIs.
- Format-specific parsing and Markdown options.
- Content-addressed image extraction with count, per-asset, and total-size limits.
- Page-level PDF scan detection and `pdf.ocr_required` diagnostics.
- Native DOCX structures, PDF coordinates/tables, and XLSX formula/merge support.

### Changed

- `DocumentAdapter.parse()` now returns `AdapterOutput`; public `parse()` remains
  backward compatible and returns only `Document`.
- `DocumentParser()` now registers built-in adapters; `adapters=[]` explicitly
  creates an adapter-free registry.
- Added permissively licensed native-format runtime dependencies.
- Bumped the package version to `0.3.0a1`; Document IR remains schema `0.1`.

## [0.2.0a1] - 2026-09-03

### Added

- Immutable Pydantic Document IR schema version 0.1.
- Public `inspect_source()`, `parse()`, and `DocumentParser` APIs.
- Path, bytes, byte-array, and binary-stream input support.
- Content-based PDF, DOCX, and XLSX detection.
- ZIP traversal, encryption, expansion, entry-count, symlink, and compression-ratio checks.
- Immutable format-adapter registry and typed exception hierarchy.

### Changed

- Added Pydantic 2 as the first runtime dependency.
- Bumped the package version to `0.2.0a1`.

## [0.1.0a1] - 2026-09-03

### Added

- Initial typed Python package scaffold.
- Apache 2.0 licensing and contribution policies.
- Cross-platform test, lint, type-check, build, license, and SBOM checks.

[Unreleased]: https://github.com/Mardaliyeva/document-parser/compare/v0.4.0-alpha.1...HEAD
[0.4.0a1]: https://github.com/Mardaliyeva/document-parser/compare/v0.3.0-alpha.1...v0.4.0-alpha.1
[0.3.0a1]: https://github.com/Mardaliyeva/document-parser/compare/v0.2.0-alpha.1...v0.3.0-alpha.1
[0.2.0a1]: https://github.com/Mardaliyeva/document-parser/compare/v0.1.0-alpha.1...v0.2.0-alpha.1
[0.1.0a1]: https://github.com/Mardaliyeva/document-parser/releases/tag/v0.1.0-alpha.1
