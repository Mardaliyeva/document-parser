# Local PDF OCR

## Scope

OCR is optional, local, and limited to PDF pages. Image files are not accepted
as top-level inputs. DOCX and XLSX continue through their native adapters only.
Chunking, embeddings, vector storage, and Kontakt-specific output formats are
outside this package.

## Installation

Install the optional Python integration:

```powershell
python -m pip install "document-parser[ocr]"
```

Install a compatible PaddlePaddle 3.x CPU or GPU inference runtime separately,
following PaddlePaddle's
[platform-specific instructions](https://www.paddleocr.ai/latest/en/version3.x/paddlepaddle_installation.html).
The package does not choose a CPU/GPU wheel or third-party index on the caller's
behalf.

PaddleOCR is Apache-2.0, but its current OCR dependency tree includes the
LGPL-licensed `python-bidi` package. That reviewed transitive exception is kept
outside the base installation and documented in
[dependency-policy.md](dependency-policy.md).

The base import remains lazy. With OCR off, `paddle`, `paddleocr`,
`pypdfium2`, and model files are not imported or inspected.

## Model preparation and offline operation

Models are prepared explicitly:

```python
from document_parser import OcrProfile, prepare_ocr_models, verify_ocr_models

report = prepare_ocr_models(
    "./models",
    profiles=(OcrProfile.STRUCTURED, OcrProfile.TEXT),
    languages=("az", "en", "ru"),
)
assert report.valid

offline_report = verify_ocr_models("./models")
```

Preparation downloads only HTTPS URLs into a temporary staging directory,
applies archive/file-count/unpacked-size limits, rejects absolute paths,
traversal, links, and device entries, and atomically installs a model only after
validation. `document-parser-models.json` records source, release,
compatibility, size, SHA-256, license notice, and extracted-file hashes.

The model manifest locks the exact bytes obtained during explicit preparation.
It is not a vendor-signed upstream provenance statement; deployments requiring
supply-chain attestation should distribute an independently reviewed manifest
and model store through their own trusted artifact channel.

`parse()` and `convert()` never perform a download. Missing or changed files
raise `OcrModelNotAvailableError` before PaddleOCR is constructed.

## Modes and profiles

```python
from pathlib import Path

from document_parser import OcrMode, OcrOptions, OcrProfile, ParseOptions

options = ParseOptions(
    ocr=OcrOptions(
        mode=OcrMode.AUTO,
        profile=OcrProfile.STRUCTURED,
        languages=("az", "en", "ru"),
        model_store=Path("models"),
        device="cpu",
        dpi=300,
    )
)
```

- `OFF` is the default and exactly preserves native Phase 3 behavior.
- `AUTO` uses only native PDF pages marked `scan_candidate=True`.
- `FORCE` OCRs every non-empty PDF page and is intended for controlled cases.
- `STRUCTURED` enables document orientation/unwarping, layout, reading order,
  titles, figures, and table structure.
- `TEXT` runs the lighter detection/recognition path and does not guarantee a
  structured table.

The default CPU reference limits are 200 pages, 40 million pixels per rendered
page, and 500 million pixels per document. Pages render at 300 DPI with PDF
rotation and alpha flattened onto white. Rasterized page images are temporary
in-memory values and never enter the asset bundle.

## Language strategy

The built-in engine uses PP-OCRv6 small/medium models for Azerbaijani and
English and `eslav_PP-OCRv5_mobile_rec` for Russian. Each detected crop is
checked by both recognition paths when Russian is enabled. Latin/Cyrillic script
fit and a fixed confidence tie threshold choose the retained text. Ambiguous or
low-confidence results remain in IR and produce review diagnostics rather than
being silently discarded.

## IR and Markdown behavior

Engine results are validated as `OcrPageResult` before mapping. Titles become
headings, text becomes paragraphs, detected markers become lists, reliable
grids become tables, and figure/caption regions retain their semantic position.
Every emitted OCR block records page number, a PDF-point bounding box,
confidence, engine, model inventory, profile, and
`extraction_method="ocr"`.

When OCR returns usable text, native blocks remain in IR and the reconciliation
stage compares text, coordinates, and confidence. It selects one active source
for overlapping duplicates/conflicts while preserving non-overlapping native
content. Markdown avoids duplicate text without discarding provenance. A
full-page scan image is hidden from Markdown but retained as an asset. If OCR
returns no text, native content remains active as a fallback and the page is
marked `needs_review`.

Important diagnostics include:

- `ocr.applied`
- `ocr.low_confidence`
- `ocr.no_text_detected`
- `ocr.language_ambiguous`
- `ocr.table_unstructured`
- `ocr.page_failed`

Dependency, model, and configuration errors are document-level typed
exceptions. A single inference/render failure is page-scoped, preserves native
content, and makes the document `partial`. Status priority is `partial`, then
`needs_review`, then `complete`.

## Custom engines

Applications may inject another local or test engine without importing
PaddleOCR:

```python
from document_parser import DocumentParser, OcrMode, OcrOptions, ParseOptions

parser = DocumentParser(
    options=ParseOptions(ocr=OcrOptions(mode=OcrMode.AUTO)),
    ocr_engine=my_engine,
)
result = parser.convert("scan.pdf")
```

The engine must implement `name` and
`recognize(OcrPageInput, OcrOptions) -> OcrPageResult`. It receives bounded PNG
bytes, owns no source stream, and must return engine-neutral validated data.

## Reproducibility boundary

Block ordering, IDs, coordinate conversion, diagnostics, IR mapping, and
Markdown serialization are deterministic for fixed validated engine output.
Byte-identical ML output is expected only with the same models, Paddle/PaddleOCR
versions, device, and runtime backend; CPU and GPU results are not asserted to
be identical.
