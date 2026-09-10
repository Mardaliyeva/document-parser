# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Simplify the local UI to upload, automatic conversion and Markdown download.
- Use AUTO/STRUCTURED AZ+EN+RU OCR and CLEAN output in the UI only; preserve
  Python/CLI defaults and advanced options.
- Move diagnostics, model verification, IR and asset downloads into collapsed
  technical details; document routing, dependencies and supported-format limits.

## [0.8.0a1] — 2026-09-08

### Changed

- Default to clean Markdown; add explicit fidelity and record-table presentation.
- Preserve whitespace across formatting runs and original normalization provenance.
- Resolve DOCX numbering identities, restarts and numbered heading semantics.
- Traverse DOCX content controls and cached fields instead of silently omitting
  stored table-of-contents entries.
- Extract PDF words independently of font subsets, preserve inline styles, group
  paragraphs/lists, assign table glyphs by center, and cluster grid coordinates.
- Identify suspicious native text on scanned pages for selective local OCR.
- Preserve the first repeated PDF header table and hide subsequent duplicates.
- Replace implicit perfect native confidence with conservative extraction quality;
  exclude unresolved OCR pages from successful coverage.
- Add CLI/UI Markdown profiles and an anonymous, local-only ZIP audit tool.
- Include PaddleOCR doc-parser extras for STRUCTURED OCR and map wrapped
  dependency initialization failures to a typed OCR dependency error.
- Bind structured model names and directories explicitly, disable unprepared
  region detection, and cluster approximate OCR cell boundaries.

### Acceptance notes

- Real printed-reference tests enforce a 2% CER target; the TEXT profile has a
  known failing reference while STRUCTURED passes that same reference. This is
  not a claim of corpus-wide OCR accuracy.
- The expanded OCR dependency tree requires policy review; consult
  `docs/dependency-policy.md` before distributing the optional extra.

### Compatibility

- IR remains 0.2. OCR remains optional/OFF. No downstream integration is updated.
- Raw HTML table/source-comment options require the fidelity profile.

## [0.7.0a1] - 2026-09-04

### Added

- Optional Apache-2.0 Gradio 6.x extra and Azerbaijani single-document test interface.
- Localhost-only `document-parser ui` command with model-store, port, and browser controls.
- Markdown, IR, quality, diagnostic, asset, gallery, and download result views.
- Short-lived per-session artifact storage and deterministic full-bundle ZIP generation.
- Typed `UiDependencyNotAvailableError` with stable `ui_dependency_not_available` code.
- UI-specific cross-platform smoke tests, license report, and CycloneDX SBOM checks.

### Changed

- Bumped the package version to `0.7.0a1`; Document IR remains schema `0.2`.
- Extended the full test suite to cover the optional local UI at 100% branch coverage.

## [0.6.0a1] - 2026-09-04

### Added

- Dependency-free `document-parser` CLI with convert, inspect, model, and version commands.
- Deterministic multi-file and directory batch conversion with isolated failures.
- Atomic full bundles containing Markdown, IR JSON, manifest JSON, and assets.
- Stable batch report schema, quality-gate exit codes, concurrency, and fail-fast behavior.
- Tag-gated GitHub/PyPI release workflow and manual/weekly real PaddleOCR smoke workflow.

### Changed

- Bumped the package version to `0.6.0a1`.
- Updated documentation for CLI operations, bundle safety, and release behavior.

## [0.5.0a1] - 2026-09-04

### Added

- Page-local native/OCR reconciliation using geometry, normalized text, and confidence.
- Conservative Unicode, whitespace, span, heading, and repeated-margin normalization.
- Deterministic coverage, confidence, structure, and fidelity quality scoring.
- Public `NormalizationOptions`, `QualityOptions`, `normalize_document()`, and
  `assess_quality()` interfaces.
- Immutable document, page, and worksheet quality report models.

### Changed

- Document IR moved to schema `0.2`; legacy schema `0.1` payloads remain readable.
- Parser output now runs reconciliation, normalization, and quality assessment before Markdown.
- Unresolved conflicts and quality thresholds participate in `needs_review` status.


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

[Unreleased]: https://github.com/Mardaliyeva/document-parser/compare/v0.7.0-alpha.1...HEAD
[0.7.0a1]: https://github.com/Mardaliyeva/document-parser/compare/v0.6.0-alpha.1...v0.7.0-alpha.1
[0.6.0a1]: https://github.com/Mardaliyeva/document-parser/compare/v0.5.0-alpha.1...v0.6.0-alpha.1
[0.5.0a1]: https://github.com/Mardaliyeva/document-parser/compare/v0.4.0-alpha.1...v0.5.0-alpha.1
[0.4.0a1]: https://github.com/Mardaliyeva/document-parser/compare/v0.3.0-alpha.1...v0.4.0-alpha.1
[0.3.0a1]: https://github.com/Mardaliyeva/document-parser/compare/v0.2.0-alpha.1...v0.3.0-alpha.1
[0.2.0a1]: https://github.com/Mardaliyeva/document-parser/compare/v0.1.0-alpha.1...v0.2.0-alpha.1
[0.1.0a1]: https://github.com/Mardaliyeva/document-parser/releases/tag/v0.1.0-alpha.1
