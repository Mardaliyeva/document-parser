"""Dependency-free command-line interface for document-parser."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from document_parser.__about__ import __version__
from document_parser.batch import BatchItemStatus, BatchOptions, convert_batch
from document_parser.exceptions import DocumentParserError, UnsafeDocumentError
from document_parser.ocr import OcrMode, OcrOptions, OcrProfile
from document_parser.ocr_models import prepare_ocr_models, verify_ocr_models
from document_parser.parser import DocumentParser
from document_parser.sources import ParseOptions


def _positive_jobs(value: str) -> int:
    try:
        jobs = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("jobs must be an integer") from exc
    if not 1 <= jobs <= 32:
        raise argparse.ArgumentTypeError("jobs must be between 1 and 32")
    return jobs


def _csv(value: str) -> tuple[str, ...]:
    values = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("at least one value is required")
    return values


def _ocr_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ocr", choices=tuple(OcrMode), default=OcrMode.OFF)
    parser.add_argument("--ocr-profile", choices=tuple(OcrProfile), default=OcrProfile.STRUCTURED)
    parser.add_argument("--languages", type=_csv, default=("az", "en", "ru"))
    parser.add_argument("--model-store", type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="document-parser",
        description="Convert DOCX, PDF, and XLSX files into deterministic RAG-friendly bundles.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert = subparsers.add_parser("convert", help="convert files or directories")
    convert.add_argument("inputs", nargs="+")
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--recursive", action="store_true")
    convert.add_argument("--jobs", type=_positive_jobs, default=1)
    convert.add_argument("--overwrite", action="store_true")
    convert.add_argument("--fail-fast", action="store_true")
    convert.add_argument("--fail-on-review", action="store_true")
    _ocr_arguments(convert)

    inspect = subparsers.add_parser("inspect", help="inspect one source without parsing")
    inspect.add_argument("input")

    models = subparsers.add_parser("models", help="prepare or verify local OCR models")
    model_commands = models.add_subparsers(dest="model_command", required=True)
    prepare = model_commands.add_parser("prepare", help="download and verify OCR models")
    prepare.add_argument("--target", type=Path)
    prepare.add_argument(
        "--profiles",
        type=_csv,
        default=(OcrProfile.STRUCTURED.value, OcrProfile.TEXT.value),
    )
    prepare.add_argument("--languages", type=_csv, default=("az", "en", "ru"))
    verify = model_commands.add_parser("verify", help="verify an offline OCR model store")
    verify.add_argument("--target", type=Path)

    subparsers.add_parser("version", help="print the installed package version")
    return parser


def _parse_options(arguments: argparse.Namespace) -> ParseOptions:
    return ParseOptions(
        ocr=OcrOptions(
            mode=OcrMode(arguments.ocr),
            profile=OcrProfile(arguments.ocr_profile),
            languages=tuple(arguments.languages),
            model_store=arguments.model_store,
        )
    )


def _run_convert(arguments: argparse.Namespace) -> int:
    report = convert_batch(
        arguments.inputs,
        arguments.output,
        parse_options=_parse_options(arguments),
        batch_options=BatchOptions(
            recursive=arguments.recursive,
            jobs=arguments.jobs,
            overwrite=arguments.overwrite,
            fail_fast=arguments.fail_fast,
        ),
    )
    print(report.model_dump_json(indent=2))
    if any(item.error_code == "unsafe_document" for item in report.items):
        return 3
    if not report.succeeded:
        return 1
    if arguments.fail_on_review and any(
        item.status in {BatchItemStatus.NEEDS_REVIEW, BatchItemStatus.PARTIAL}
        for item in report.items
    ):
        return 1
    return 0


def _run_models(arguments: argparse.Namespace) -> int:
    if arguments.model_command == "prepare":
        profiles = tuple(OcrProfile(item) for item in arguments.profiles)
        report = prepare_ocr_models(
            arguments.target, profiles=profiles, languages=arguments.languages
        )
    else:
        report = verify_ocr_models(arguments.target)
    print(report.model_dump_json(indent=2))
    return 0 if report.valid else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a stable process exit code."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "version":
            print(__version__)
            return 0
        if arguments.command == "inspect":
            result = DocumentParser().inspect(arguments.input)
            print(result.model_dump_json(indent=2))
            return 0
        if arguments.command == "models":
            return _run_models(arguments)
        return _run_convert(arguments)
    except UnsafeDocumentError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (ValidationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (DocumentParserError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through the console entry point
    raise SystemExit(main())
