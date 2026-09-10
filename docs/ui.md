# Local document inspection UI

The optional UI is an Azerbaijani, localhost-only test surface for inspecting
one DOCX, PDF, or XLSX conversion at a time. It is not a hosted application and
does not add a second parsing pipeline: every conversion passes through
`DocumentParser.convert()` and the existing atomic bundle writer.

## Installation

Install only the UI when testing native DOCX, born-digital PDF, and XLSX files:

```powershell
python -m pip install -e ".[ui]"
```

Install both extras for scanned and mixed PDFs:

```powershell
python -m pip install -e ".[ui,ocr]"
```

PaddlePaddle 3.x remains a separate platform-specific installation. Prepare
the required model files before enabling OCR; conversion never downloads a
model:

```powershell
document-parser models prepare --target ./models --profiles structured,text --languages az,en,ru
document-parser models verify --target ./models
```

See [ocr.md](ocr.md) for CPU/GPU installation and offline model-store details.

## Launch

```powershell
document-parser ui
document-parser ui --model-store ./models
document-parser ui --port 7860
document-parser ui --no-browser
```

`--port` accepts values from 1 to 65535. Without it, Gradio starts at its
default port 7860 and searches for a free local port. `--no-browser` prevents
automatic browser launch and prints the local address in the terminal.

There is deliberately no `--host` or `--share` flag. Launch always uses:

- `server_name="127.0.0.1"`;
- `share=False`;
- strict CORS checks;
- disabled analytics and run history;
- no footer links;
- private callback endpoints and a closed queue API;
- one concurrent conversion and a queue limit of four.

The interface therefore cannot intentionally bind to the LAN or create a
Gradio public-share URL.

## Workflow

1. Select one DOCX, PDF or XLSX file (maximum 100 MiB).
2. Press **Markdown-a çevir**. Upload alone does not start parsing.
3. Read the Markdown preview and localized status.
4. Press **Markdown-u endir**.
5. Open **Texniki məlumatlar** only when diagnostics, IR, assets or ZIP are needed.
6. Press **Təmizlə** to remove the previous result.

There are no conversion configuration controls. The controller calls the existing
parser with AUTO OCR, STRUCTURED profile, AZ/EN/RU, CLEAN Markdown, automatic
tables and displayed formula values. Source comments, repeated headers/footers
and hidden sheets are excluded from Markdown. Advanced settings remain in the
Python API and CLI, whose OCR OFF defaults are unchanged.

Content-based preflight routes DOCX/PDF/XLSX. DOCX and XLSX use their native
adapters; PDF selection is per page. A logo alone does not require OCR.
Scan candidates and suspicious text layers are evaluated by existing PDF
heuristics, not by a blanket "any image means OCR" rule. These heuristics are
not guaranteed to detect every damaged/vector-text page.

OCR needs separately installed packages, a compatible PaddlePaddle runtime
and verified local models. Missing prerequisites produce a clear error when a
candidate page needs OCR. Conversion never downloads models or uses cloud OCR.
The model verification button in technical details only checks local files.
The model-store path is selected at launch, not entered in the browser.

## Result views

The summary shows the content-derived format, source name, localized status,
overall quality, coverage, confidence, structure, fidelity, document ID, asset
count, and diagnostic count.

Status labels are mapped as follows:

| IR status | UI label |
| --- | --- |
| `complete` | Tamamlandı |
| `needs_review` | Yoxlama tələb edir |
| `partial` | Qismən tamamlandı |
| fatal exception | Çevrilmə uğursuz oldu |

The tabs contain:

- a sanitized rendered Markdown preview;
- raw canonical Markdown;
- diagnostic severity, stable code, message, and source location;
- complete Document IR 0.2 JSON;
- the asset manifest and a gallery limited to `image/*` MIME types.

Expected parser failures expose the same stable error code as the Python API.
Unexpected failures display only `internal_error`; their traceback is written
to the local terminal log and never inserted into browser output. A failed
conversion clears every view and download from the previous document.

## Downloads and deterministic ZIP

The three download controls return:

- `document.md`;
- `document.json`;
- `<safe-stem>-<sha256-prefix>.zip`.

The ZIP contains one normal CLI-style bundle:

```text
<safe-stem>-<sha256-prefix>/
├── document.md
├── document.json
├── manifest.json
└── assets/
    └── <content-addressed-files>
```

Entries are sorted, relative, symlink-free, and written with a fixed timestamp
and fixed permissions. The uploaded source is never copied into the ZIP. For
the same generated bundle, ZIP bytes are deterministic.

## Temporary files and cleanup

Each UI process owns a dedicated temporary root. Each browser session state
references only a random job identifier registered by the server; it never
contains or accepts an output path from the browser.

A job is deleted when:

- the same session starts a new conversion;
- **Təmizlə** is pressed;
- Gradio releases the session state;
- its age exceeds one hour during periodic cleanup;
- the server exits normally or receives an interrupt.

Gradio's upload cache is checked every five minutes and files older than one
hour are expired. No broad `allowed_paths` value is configured. Download and
gallery paths point only to files generated inside the active job directory.

## Troubleshooting

### `ui_dependency_not_available`

Install the extra in the same Python environment that provides the
`document-parser` executable:

```powershell
python -m pip install -e ".[ui]"
```

The base package intentionally does not import or install Gradio.

### OCR model store is not ready

Healthy native documents work without OCR models. For scans, install `.[ocr]` and the
appropriate PaddlePaddle runtime, prepare the model store, verify it from the
CLI, then launch with `--model-store` pointing to the same directory.

### Address already in use

Omit `--port` so Gradio can search from 7860, or provide another local port:

```powershell
document-parser ui --port 7861
```

### The result requires review

Open the Diagnostics tab. `needs_review` is a successful conversion with a
quality or ambiguity signal, not a fatal error. `partial` means some source
content could not be fully represented. The IR preserves provenance and
inactive alternatives even when Markdown hides them.

### The browser did not open

Read the local URL printed in the terminal and open it manually. This is
expected when using `--no-browser` or when OS browser launching is restricted.

## Scope

This UI is intentionally single-document and local. Batch conversion remains
available through `document-parser convert` and the Python batch API. Hosting,
authentication, source retention, chunking, embeddings, vector databases, and
Kontakt AI integration remain outside this package.
