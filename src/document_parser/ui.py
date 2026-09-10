# ruff: noqa: RUF001
"""Local-only Gradio interface for inspecting one converted document."""

from __future__ import annotations

import atexit
import json
import logging
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Sequence
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from document_parser.batch import write_conversion_bundle
from document_parser.exceptions import (
    DocumentParserError,
    UiDependencyNotAvailableError,
    UnsafeDocumentError,
)
from document_parser.markdown import FormulaMode, MarkdownOptions, MarkdownProfile, TableMode
from document_parser.models import Diagnostic, DocumentStatus, FrozenModel
from document_parser.ocr import OcrMode, OcrOptions, OcrProfile
from document_parser.ocr_models import OcrModelReport, verify_ocr_models
from document_parser.parser import DocumentParser
from document_parser.results import ConversionResult
from document_parser.sources import MEBIBYTE, ParseOptions

LOGGER = logging.getLogger(__name__)

LOCAL_SERVER_NAME = "127.0.0.1"
DEFAULT_UI_PORT = 7860
MAX_UPLOAD_BYTES = 100 * MEBIBYTE
GRADIO_CACHE_CLEANUP_SECONDS = 300
ARTIFACT_TTL_SECONDS = 3600
QUEUE_MAX_SIZE = 4
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

_STATUS_LABELS = {
    DocumentStatus.COMPLETE: "Tamamlandı",
    DocumentStatus.NEEDS_REVIEW: "Yoxlama tələb edir",
    DocumentStatus.PARTIAL: "Qismən tamamlandı",
}


class _ParserLike(Protocol):
    def convert(
        self,
        source: str | Path,
        *,
        filename: str | None = None,
        markdown_options: MarkdownOptions | None = None,
    ) -> ConversionResult: ...


ParserFactory = Callable[[ParseOptions], _ParserLike]


class UiView(FrozenModel):
    """Serializable values returned to Gradio components after one action."""

    job_id: str | None = None
    status: str = ""
    summary: str = ""
    markdown_preview: str = ""
    raw_markdown: str = ""
    diagnostics: tuple[tuple[str, str, str, str], ...] = ()
    document_json: str = ""
    assets: tuple[tuple[str, str, str, int, str], ...] = ()
    gallery: tuple[tuple[str, str], ...] = ()
    markdown_download: str | None = None
    json_download: str | None = None
    bundle_download: str | None = None

    def component_values(self) -> tuple[object, ...]:
        """Return values in the same order as the UI output components."""

        return (
            self.job_id,
            self.status,
            self.summary,
            self.markdown_preview,
            self.raw_markdown,
            [list(row) for row in self.diagnostics],
            self.document_json,
            [list(row) for row in self.assets],
            [list(item) for item in self.gallery],
            self.markdown_download,
            self.json_download,
            self.bundle_download,
        )


class _ArtifactStore:
    """Own short-lived conversion bundles without accepting paths from the browser."""

    def __init__(
        self, root: Path | None = None, *, ttl_seconds: int = ARTIFACT_TTL_SECONDS
    ) -> None:
        self._owns_root = root is None
        self.root = (
            Path(tempfile.mkdtemp(prefix="docparser-ui-")) if root is None else root.resolve()
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = ttl_seconds
        self._jobs: dict[str, Path] = {}
        self._lock = threading.RLock()

    def create_job(self) -> tuple[str, Path]:
        """Create and register an application-owned job directory."""

        with self._lock:
            job_id = f"job-{uuid4().hex[:12]}"
            path = self.root / job_id
            path.mkdir()
            self._jobs[job_id] = path
            return job_id, path

    def remove_job(self, job_id: str | None) -> None:
        """Remove only a directory registered by this store."""

        if not job_id:
            return
        with self._lock:
            path = self._jobs.pop(job_id, None)
        if path is not None and path.exists():
            shutil.rmtree(path)

    def cleanup_expired(self, *, now: float | None = None) -> None:
        """Delete registered jobs older than the configured TTL."""

        threshold = (time.time() if now is None else now) - self._ttl_seconds
        with self._lock:
            expired = tuple(
                job_id for job_id, path in self._jobs.items() if _last_modified(path) < threshold
            )
        for job_id in expired:
            self.remove_job(job_id)

    def close(self) -> None:
        """Remove every registered job and, when owned, the application temp root."""

        with self._lock:
            job_ids = tuple(self._jobs)
        for job_id in job_ids:
            self.remove_job(job_id)
        if self._owns_root and self.root.exists():
            shutil.rmtree(self.root)


def _last_modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _default_parser_factory(options: ParseOptions) -> _ParserLike:
    return DocumentParser(options=options)


def _import_optional(name: str) -> Any:
    return import_module(name)


def _load_gradio() -> Any:
    try:
        return _import_optional("gradio")
    except ImportError as exc:
        raise UiDependencyNotAvailableError(
            "Lokal interfeys üçün 'ui' optional dependency-si quraşdırılmalıdır: "
            "pip install 'document-parser[ui]'"
        ) from exc


def _safe_zip_name(path: Path, root: Path) -> str:
    if path.is_symlink():
        raise UnsafeDocumentError("bundle ZIP cannot contain symbolic links")
    relative = PurePosixPath(path.relative_to(root).as_posix())
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise UnsafeDocumentError("bundle ZIP entry is not a safe relative path")
    return relative.as_posix()


def _zip_info(name: str, *, directory: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name + ("/" if directory else ""), ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o755 if directory else 0o644) << 16
    return info


def create_deterministic_bundle_zip(bundle_directory: Path, destination: Path) -> Path:
    """Archive a generated bundle with stable ordering, metadata, and safe entry names."""

    bundle = bundle_directory.resolve()
    if not bundle.is_dir() or bundle.is_symlink():
        raise UnsafeDocumentError("bundle ZIP source must be a normal directory")
    entries = sorted(bundle.rglob("*"), key=lambda path: path.relative_to(bundle).as_posix())
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        root_name = f"{bundle.name}/"
        archive.writestr(_zip_info(root_name.rstrip("/"), directory=True), b"")
        for path in entries:
            relative_name = _safe_zip_name(path, bundle)
            archive_name = f"{bundle.name}/{relative_name}"
            if path.is_dir():
                archive.writestr(_zip_info(archive_name, directory=True), b"")
            elif path.is_file():
                archive.writestr(_zip_info(archive_name, directory=False), path.read_bytes())
            else:  # pragma: no cover - rglob exposes only regular filesystem entries here
                raise UnsafeDocumentError("bundle ZIP contains a non-regular entry")
    return destination


def _diagnostic_location(diagnostic: Diagnostic) -> str:
    if diagnostic.location is None:
        return ""
    return json.dumps(
        diagnostic.location.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
    )


def _format_score(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def _summary(result: ConversionResult) -> str:
    document = result.document
    quality = document.quality
    return "\n".join(
        (
            "### Nəticə xülasəsi",
            f"- **Mənbə:** `{document.source.name.replace('`', '')}`",
            f"- **Format:** `{document.source.format.value.upper()}`",
            f"- **Status:** {_STATUS_LABELS[document.status]}",
            f"- **Ümumi keyfiyyət:** {_format_score(quality.overall_score if quality else None)}",
            f"- **Əhatə:** {_format_score(quality.coverage_score if quality else None)}",
            f"- **Etibar:** {_format_score(quality.confidence_score if quality else None)}",
            f"- **Struktur:** {_format_score(quality.structure_score if quality else None)}",
            f"- **Sadiqlik:** {_format_score(quality.fidelity_score if quality else None)}",
            f"- **Document ID:** `{document.document_id}`",
            f"- **Asset sayı:** {len(document.assets)}",
            f"- **Diagnostic sayı:** {len(document.diagnostics)}",
        )
    )


def _model_report_text(report: OcrModelReport) -> str:
    if report.valid:
        return f"✅ OCR model store hazırdır — {len(report.verified_models)} model yoxlanıldı."
    details: list[str] = []
    if report.missing:
        details.append(f"çatışmayan: {', '.join(report.missing)}")
    if report.corrupted:
        details.append(f"korlanmış: {', '.join(report.corrupted)}")
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"⚠️ OCR model store hazır deyil{suffix}."


class UiController:
    """Convert one upload and map the result to safe, localized UI values."""

    def __init__(
        self,
        *,
        model_store: Path | None = None,
        artifact_root: Path | None = None,
        parser_factory: ParserFactory = _default_parser_factory,
        artifact_ttl_seconds: int = ARTIFACT_TTL_SECONDS,
    ) -> None:
        self.model_store = model_store.resolve() if model_store is not None else None
        self._artifacts = _ArtifactStore(artifact_root, ttl_seconds=artifact_ttl_seconds)
        self._parser_factory = parser_factory
        self._parser_key: str | None = None
        self._parser: _ParserLike | None = None
        self._lock = threading.RLock()
        self._closed = False

    @property
    def artifact_root(self) -> Path:
        return self._artifacts.root

    def model_status(self) -> str:
        """Return a localized, non-mutating model-store verification summary."""

        try:
            return _model_report_text(verify_ocr_models(self.model_store))
        except DocumentParserError as exc:
            return f"⚠️ OCR model vəziyyəti yoxlanmadı: {exc}"
        except OSError:
            return "⚠️ OCR model vəziyyəti yoxlanmadı: model store oxunmadı."

    def cleanup_expired(self) -> None:
        self._artifacts.cleanup_expired()

    def remove_job(self, job_id: str | None) -> None:
        self._artifacts.remove_job(job_id)

    def clear(self, previous_job_id: str | None) -> tuple[object, ...]:
        """Clear the current session result and its generated files."""

        self.remove_job(previous_job_id)
        return UiView().component_values()

    def clear_with_upload(self, previous_job_id: str | None) -> tuple[object, ...]:
        """Clear the upload component together with all generated result values."""

        return (None, *self.clear(previous_job_id))

    def close(self) -> None:
        """Release parser references and application-owned temporary files."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._parser = None
            self._parser_key = None
        self._artifacts.close()

    def _get_parser(self, options: ParseOptions) -> _ParserLike:
        key = options.model_dump_json()
        with self._lock:
            if self._closed:
                raise RuntimeError("UI controller is closed")
            if self._parser is None or self._parser_key != key:
                self._parser = self._parser_factory(options)
                self._parser_key = key
            return self._parser

    def _options(
        self,
        ocr_mode: str,
        ocr_profile: str,
        languages: Sequence[str],
    ) -> ParseOptions:
        return ParseOptions(
            ocr=OcrOptions(
                mode=OcrMode(ocr_mode),
                profile=OcrProfile(ocr_profile),
                languages=tuple(language.lower() for language in languages),
                model_store=self.model_store,
            )
        )

    @staticmethod
    def _markdown_options(
        include_source_markers: bool,
        include_headers_footers: bool,
        include_hidden_sheets: bool,
        formula_mode: str,
        table_mode: str,
        markdown_profile: str = "clean",
    ) -> MarkdownOptions:
        return MarkdownOptions(
            profile=MarkdownProfile(markdown_profile),
            include_source_markers=include_source_markers,
            include_headers_footers=include_headers_footers,
            include_hidden_sheets=include_hidden_sheets,
            formula_mode=FormulaMode(formula_mode),
            table_mode=TableMode(table_mode),
        )

    def convert(
        self,
        upload_path: str | Path | None,
        ocr_mode: str,
        ocr_profile: str,
        languages: Sequence[str],
        include_source_markers: bool,
        include_headers_footers: bool,
        include_hidden_sheets: bool,
        formula_mode: str,
        table_mode: str,
        previous_job_id: str | None,
        markdown_profile: str = "clean",
    ) -> tuple[object, ...]:
        """Run the canonical parser and return component-ready values."""

        self.remove_job(previous_job_id)
        if upload_path is None:
            return UiView(status="⚠️ Əvvəlcə DOCX, PDF və ya XLSX faylı seçin.").component_values()
        source = Path(upload_path)
        try:
            self.cleanup_expired()
            options = self._options(ocr_mode, ocr_profile, languages)
            markdown_options = self._markdown_options(
                include_source_markers,
                include_headers_footers,
                include_hidden_sheets,
                formula_mode,
                table_mode,
                markdown_profile,
            )
            result = self._get_parser(options).convert(
                source,
                markdown_options=markdown_options,
            )
            view = self._write_result(result)
            return view.component_values()
        except DocumentParserError as exc:
            return UiView(
                status=(
                    f"❌ Çevrilmə uğursuz oldu — `{exc.code.value}`: {str(exc).replace('`', '')}"
                )
            ).component_values()
        except OSError:
            LOGGER.exception("local UI could not write conversion artifacts")
            return UiView(
                status=(
                    "❌ Çevrilmə uğursuz oldu — `output_write_error`: "
                    "lokal nəticə faylları yazılmadı."
                )
            ).component_values()
        except (ValidationError, ValueError) as exc:
            return UiView(
                status=f"❌ Çevrilmə uğursuz oldu — `invalid_configuration`: {str(exc).replace('`', '')}"
            ).component_values()
        except Exception:
            LOGGER.exception("unexpected local UI conversion failure")
            return UiView(
                status="❌ Çevrilmə uğursuz oldu — `internal_error`: gözlənilməyən xəta."
            ).component_values()

    def convert_auto(
        self, upload_path: str | Path | None, previous_job_id: str | None
    ) -> tuple[object, ...]:
        """Use the automatic UI policy without changing library defaults."""
        return self.convert(
            upload_path,
            "auto",
            "structured",
            ("az", "en", "ru"),
            False,
            False,
            False,
            "displayed",
            "auto",
            previous_job_id,
            "clean",
        )

    def _write_result(self, result: ConversionResult) -> UiView:
        job_id, job_path = self._artifacts.create_job()
        try:
            bundle = write_conversion_bundle(result, job_path)
            zip_path = create_deterministic_bundle_zip(
                bundle,
                job_path / f"{bundle.name}.zip",
            )
            document_path = bundle / "document.json"
            markdown_path = bundle / "document.md"
            diagnostics = tuple(
                (
                    item.severity.value,
                    item.code,
                    item.message,
                    _diagnostic_location(item),
                )
                for item in result.document.diagnostics
            )
            assets = tuple(
                (
                    item.filename,
                    item.media_type,
                    item.sha256,
                    item.size_bytes,
                    item.asset_id,
                )
                for item in result.document.assets
            )
            gallery = tuple(
                (
                    str(bundle / "assets" / item.filename),
                    item.filename,
                )
                for item in result.document.assets
                if item.media_type.startswith("image/")
            )
            return UiView(
                job_id=job_id,
                status=f"✅ {_STATUS_LABELS[result.document.status]}",
                summary=_summary(result),
                markdown_preview=result.markdown,
                raw_markdown=result.markdown,
                diagnostics=diagnostics,
                document_json=document_path.read_text(encoding="utf-8"),
                assets=assets,
                gallery=gallery,
                markdown_download=str(markdown_path),
                json_download=str(document_path),
                bundle_download=str(zip_path),
            )
        except Exception:
            self.remove_job(job_id)
            raise


def build_ui(
    *,
    model_store: Path | None = None,
    artifact_root: Path | None = None,
    parser_factory: ParserFactory = _default_parser_factory,
) -> Any:
    """Build, but do not launch, the local Azerbaijani Gradio interface."""

    gr = _load_gradio()
    controller = UiController(
        model_store=model_store,
        artifact_root=artifact_root,
        parser_factory=parser_factory,
    )
    atexit.register(controller.close)

    with gr.Blocks(
        title="Document Parser — lokal yoxlama",
        analytics_enabled=False,
        delete_cache=(GRADIO_CACHE_CLEANUP_SECONDS, ARTIFACT_TTL_SECONDS),
    ) as app:
        gr.Markdown(
            "# Document Parser — lokal sənəd yoxlaması\n"
            "DOCX, PDF və XLSX yükləyin, təmiz Markdown alın. "
            "PDF-də ehtiyac olan səhifələr avtomatik lokal OCR edilir."
        )
        job_state = gr.State(
            value=None, time_to_live=ARTIFACT_TTL_SECONDS, delete_callback=controller.remove_job
        )

        with gr.Row():
            upload = gr.File(
                label="Sənəd",
                file_types=[".docx", ".pdf", ".xlsx"],
                file_count="single",
                type="filepath",
            )
            with gr.Column():
                convert_button = gr.Button("Markdown-a çevir", variant="primary")
                clear_button = gr.Button("Təmizlə")

        status = gr.Markdown()
        markdown_preview = gr.Markdown(sanitize_html=True)
        markdown_download = gr.DownloadButton("Markdown-u endir")
        with gr.Accordion("Texniki məlumatlar", open=False):
            summary = gr.Markdown()
            model_status = gr.Markdown(controller.model_status())
            refresh_models = gr.Button("Model vəziyyətini yenidən yoxla")
            with gr.Tabs():
                with gr.Tab("Raw Markdown"):
                    raw_markdown = gr.Code(language="markdown", interactive=False)
                with gr.Tab("Diagnostics"):
                    diagnostics = gr.Dataframe(
                        headers=["Səviyyə", "Kod", "Mesaj", "Mənbə yeri"],
                        datatype=["str", "str", "str", "str"],
                        interactive=False,
                    )
                with gr.Tab("Document IR"):
                    document_json = gr.Code(language="json", interactive=False)
                with gr.Tab("Asset-lər"):
                    assets = gr.Dataframe(
                        headers=["Fayl", "MIME", "SHA-256", "Ölçü", "Asset ID"],
                        datatype=["str", "str", "str", "number", "str"],
                        interactive=False,
                    )
                    gallery = gr.Gallery(label="Şəkil asset-ləri", columns=3, object_fit="contain")
            with gr.Row():
                json_download = gr.DownloadButton("document.json yüklə")
                bundle_download = gr.DownloadButton("bundle.zip yüklə")

        inputs = [upload, job_state]
        outputs = [
            job_state,
            status,
            summary,
            markdown_preview,
            raw_markdown,
            diagnostics,
            document_json,
            assets,
            gallery,
            markdown_download,
            json_download,
            bundle_download,
        ]
        convert_button.click(
            controller.convert_auto,
            inputs=inputs,
            outputs=outputs,
            concurrency_limit=1,
            api_visibility="private",
        )
        clear_button.click(
            controller.clear_with_upload,
            inputs=[job_state],
            outputs=[upload, *outputs],
            concurrency_limit=1,
            api_visibility="private",
        )
        refresh_models.click(
            controller.model_status,
            outputs=[model_status],
            concurrency_limit=1,
            api_visibility="private",
        )
        timer = gr.Timer(value=GRADIO_CACHE_CLEANUP_SECONDS, active=True)
        timer.tick(controller.cleanup_expired, api_visibility="private")
        app.unload(controller.cleanup_expired)

    app.queue(api_open=False, default_concurrency_limit=1, max_size=QUEUE_MAX_SIZE)
    app._document_parser_controller = controller
    return app


def launch_ui(
    *,
    model_store: Path | None = None,
    port: int | None = None,
    inbrowser: bool = True,
) -> None:
    """Launch the interface on localhost with sharing and API pages disabled."""

    app = build_ui(model_store=model_store)
    controller = app._document_parser_controller
    try:
        app.launch(
            server_name=LOCAL_SERVER_NAME,
            server_port=port,
            share=False,
            inbrowser=inbrowser,
            strict_cors=True,
            max_file_size=f"{MAX_UPLOAD_BYTES // MEBIBYTE}mb",
            footer_links=[],
            run_history=False,
        )
    finally:
        controller.close()
