# Synthetic document corpus

This directory contains small, deterministic integration fixtures for the public
`document-parser` pipeline. The corpus covers the three supported formats and the
main PDF extraction paths:

- rich DOCX structure and embedded media;
- born-digital PDF text, layout, table, and two-column content;
- image-only, mixed, and rotated scan PDFs;
- an encrypted PDF negative case;
- XLSX formulas, typed values, regions, merges, multiple sheets, and an image.

`manifest.json` records the immutable size and SHA-256 of each binary fixture.
Tests use a deterministic injected OCR engine for normal CI, so PaddleOCR models
are not downloaded during the base test suite. The opt-in real OCR test uses the
same image-only scan when a verified model store and Paddle runtime are available.

The files contain synthetic facts only. They do not contain personal or production
data.
