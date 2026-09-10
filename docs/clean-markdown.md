# Clean Markdown and extraction quality (0.8.0a1)

`convert()` and `to_markdown()` now default to `MarkdownProfile.CLEAN`.
The IR schema remains 0.2. This is a presentation-default change, not a
promise that every PDF or scan is accurately readable.

```python
from pathlib import Path
from document_parser import (
    convert,
    MarkdownOptions,
    MarkdownProfile,
    OcrMode,
    OcrOptions,
    OcrProfile,
    ParseOptions,
)

result = convert("document.docx")
print(result.markdown)
print(result.document.status, result.document.quality)

# Legacy presentation: HTML tables, underline, source markers and image links.
fidelity = convert(
    "document.docx",
    markdown_options=MarkdownOptions(profile=MarkdownProfile.FIDELITY),
)

# OCR is optional and still OFF by default. Prepare local models explicitly first.
scanned = convert(
    "scan.pdf",
    options=ParseOptions(
        ocr=OcrOptions(
            mode=OcrMode.AUTO,
            profile=OcrProfile.STRUCTURED,
            languages=("az", "en", "ru"),
            model_store=Path("models"),
        )
    ),
)
```

## Presentation contract

- Clean output never generates raw HTML, page comments or image links.
- Source text resembling HTML is escaped as literal text, not deleted.
- Underline is omitted; its text and other inline formatting are preserved.
- Simple header-bearing tables use GFM. Complex tables use row records with
  header paths and merged-cell context. Blank form fields retain their labels.
- `TableMode.RECORDS` explicitly requests records. In clean mode, forcing GFM
  does not flatten a complex table and lose its merged relationships.
- Source comments and `TableMode.HTML` require fidelity; contradictory clean
  options raise a Pydantic validation error.
- Images remain in the IR/asset bundle. Useful captions remain in clean text.
- Exact repeated PDF header tables are hidden after their first occurrence.
- No spelling correction, semantic paraphrasing or invented transcription occurs.
- DOCX content controls, cached field text and inserted block wrappers are traversed;
  a stored table of contents is not silently omitted by the native iterator.

```console
document-parser convert document.pdf --output output --profile clean --fail-on-review
document-parser convert document.pdf --output output --profile fidelity
document-parser ui
```

The local UI offers the same clean/fidelity profile. Source markers default
to disabled; enable them only with fidelity. OCR selection is independent.

## Quality and uncertainty

Native extraction quality is a heuristic, not a calibrated OCR probability.
Unmeasured native text defaults conservatively to 0.95 rather than 1.0;
PDF mixed-script/damaged text evidence may lower it. This score alone cannot
prove correct transcription. Page diagnostics and `needs_review`/`partial`
must also be checked. A high document average cannot clear unresolved OCR.

AUTO examines image coverage and native-text evidence, including suspicious
mixed-script tokens on scanned pages. Separate Latin and Cyrillic words are
not themselves corruption. A nonempty but damaged OCR layer can require OCR.
Missing optional dependencies/models fail explicitly when OCR is required.
The OCR extra includes PaddleOCR's `doc-parser` dependencies required by
PP-StructureV3, not only the default text-only `ocr-core` dependencies.
No document or page is uploaded to a cloud service.

Handwritten dates, signatures and uncertain table geometry require review;
do not treat best-effort Markdown as verified ground truth.

## Local corpus audits

```console
python scripts/audit_corpus.py private.zip --output ../private-audit --verify-determinism
python scripts/audit_corpus.py private.zip --output ../private-ocr-audit --ocr auto --model-store ../models
```

The audit records hashes, options, diagnostics, quality, block counts and
determinism alongside Markdown and IR. It rejects output inside this repository.
Never commit private corpora, source names, extracted text, signatures, images
or audit output. Public tests must use anonymous synthetic examples.

Acceptance requires source-to-output comparison, including critical numeric
fields and table cells. Unit-test coverage and syntactically clean Markdown
are not substitutes for that comparison. Real OCR needs a separately prepared
runtime/model store and measured transcription references.

## Integration boundary

This change is confined to the library. Consumers pinned to an older Git
commit keep their old behavior until their dependency is explicitly updated.
Chunk overlap, search indexing and consumer preview reconstruction are not
changed here.
