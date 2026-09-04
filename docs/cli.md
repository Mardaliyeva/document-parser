# CLI and bundle format

## Commands

The package installs the `document-parser` console command without adding a CLI
framework dependency.

```text
document-parser convert <file-or-directory>... --output <directory>
document-parser inspect <file>
document-parser models prepare [--target <directory>]
document-parser models verify [--target <directory>]
document-parser version
```

`convert` accepts explicit files and directories. Directory discovery selects
DOCX, PDF, and XLSX files. `--recursive` includes nested directories. Explicit
files are content-detected and can return typed unsupported or unsafe failures.

OCR is opt-in:

```powershell
document-parser convert scanned.pdf --output converted `
  --ocr auto --ocr-profile structured `
  --languages az,en,ru --model-store models
```

`--jobs` is limited to 1–32 and defaults to one. Parallel reports remain sorted
by normalized source path. `--fail-fast` is sequential and stops after the first
failed item. Every worker owns its parser and OCR engine, so more jobs can
multiply model memory usage.

## Bundle layout

```text
<output>/
├── <safe-stem>-<source-sha256-prefix>/
│   ├── document.md
│   ├── document.json
│   ├── manifest.json
│   └── assets/
└── batch-report.json
```

`document.json` is the loss-preserving IR. `manifest.json` contains source
identity, status, quality, diagnostics, and assets. `batch-report.json` contains
a deterministic entry for every attempted source and excludes timing data.

Bundles are written in a temporary directory on the output filesystem and
renamed into place only after completion. Existing bundles require
`--overwrite`; replacement uses a rollback directory. Input symlinks, output
symlink components, unsafe asset names, and bundle path escapes are rejected.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every selected input produced a bundle. |
| `1` | Conversion/write failed, or `--fail-on-review` rejected a review/partial result. |
| `2` | CLI arguments or configuration are invalid. |
| `3` | An unsafe input or output condition was detected. |

`needs_review` and `partial` are successful conversion states by default. Use
`--fail-on-review` when either state must fail an automated quality gate.

## Release workflow

The release workflow runs only for a `v*` tag. It checks tag/version equality,
reruns quality gates, builds wheel/sdist files, performs a clean-wheel smoke
test, and creates SHA-256 checksums and a CycloneDX runtime SBOM. PyPI upload
uses trusted publishing through the protected `pypi` GitHub Environment.
Creating the workflow alone does not create a tag or publish a package.
