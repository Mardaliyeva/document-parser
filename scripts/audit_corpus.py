"""Audit private ZIP inputs locally; keep output outside this repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from document_parser import (
    ContainerBlock,
    ContentBlock,
    DocumentParser,
    ListBlock,
    OcrMode,
    OcrOptions,
    OcrProfile,
    ParseOptions,
    TableBlock,
    __version__,
)


def walk(blocks: tuple[ContentBlock, ...]) -> Iterator[ContentBlock]:
    for block in blocks:
        yield block
        if isinstance(block, ContainerBlock):
            yield from walk(block.blocks)
        elif isinstance(block, ListBlock):
            for item in block.items:
                yield from walk(item.blocks)
        elif isinstance(block, TableBlock):
            for row in block.rows:
                for cell in row.cells:
                    yield from walk(cell.blocks)


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("archive", type=Path)
    cli.add_argument("--output", required=True, type=Path)
    cli.add_argument("--ocr", choices=tuple(OcrMode), default="off")
    cli.add_argument("--ocr-profile", choices=tuple(OcrProfile), default="structured")
    cli.add_argument("--model-store", type=Path)
    cli.add_argument("--verify-determinism", action="store_true")
    args = cli.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(Path(__file__).resolve().parents[1]):
        cli.error("private output must be outside the repository")
    output.mkdir(parents=True, exist_ok=True)
    options = ParseOptions(
        ocr=OcrOptions(mode=args.ocr, profile=args.ocr_profile, model_store=args.model_store)
    )
    parser = DocumentParser(options=options)
    report: dict[str, Any] = {
        "version": __version__,
        "options": options.model_dump(mode="json"),
        "files": [],
    }
    with ZipFile(args.archive) as archive:
        for index, entry in enumerate(sorted(archive.infolist(), key=lambda e: e.filename)):
            if Path(entry.filename).suffix.lower() not in {".docx", ".pdf", ".xlsx"}:
                continue
            if (
                entry.file_size > options.max_input_bytes
                or entry.file_size / max(1, entry.compress_size) > 100
            ):
                raise ValueError("unsafe corpus entry")
            data = archive.read(entry)
            row: dict[str, Any] = {
                "source": entry.filename,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            try:
                result = parser.convert(data, filename=Path(entry.filename).name)
                blocks = tuple(walk(result.document.blocks))
                ir = result.document.model_dump_json(indent=2)
                (output / f"{index:02}.md").write_text(
                    result.markdown, encoding="utf-8", newline="\n"
                )
                (output / f"{index:02}.json").write_text(ir, encoding="utf-8", newline="\n")
                row.update(
                    {
                        "status": result.document.status.value,
                        "quality": result.document.quality.model_dump(mode="json")
                        if result.document.quality
                        else None,
                        "diagnostics": [
                            d.model_dump(mode="json") for d in result.document.diagnostics
                        ],
                        "markdown_characters": len(result.markdown),
                        "html": len(
                            re.findall(
                                r"(?<!\\)</?(?:table|tr|td|th|u|br)\b|<!--|!\[", result.markdown
                            )
                        ),
                        "blocks": {
                            kind: sum(b.type == kind for b in blocks)
                            for kind in ("heading", "paragraph", "list", "table", "figure")
                        },
                        "ocr_candidates": [
                            b.source.page_number
                            for b in blocks
                            if isinstance(b, ContainerBlock)
                            and b.source
                            and b.attributes.get("scan_candidate")
                        ],
                    }
                )
                if args.verify_determinism:
                    again = parser.convert(data, filename=Path(entry.filename).name)
                    row["deterministic"] = (
                        again.markdown == result.markdown
                        and again.document.model_dump_json(indent=2) == ir
                    )
            except Exception as exc:
                row.update({"status": "failed", "error": type(exc).__name__, "message": str(exc)})
            report["files"].append(row)
            print(json.dumps(row, ensure_ascii=True), flush=True)
            (output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return int(any(row["status"] == "failed" for row in report["files"]))


if __name__ == "__main__":
    raise SystemExit(main())
