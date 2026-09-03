"""Shared deterministic helpers for built-in adapters."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import unicodedata
from collections.abc import Iterable
from pathlib import PurePosixPath

from document_parser.exceptions import UnsafeDocumentError
from document_parser.models import AssetRef
from document_parser.results import AssetPayload
from document_parser.sources import ParseOptions

_SAFE_SUFFIX = re.compile(r"^\.[a-z0-9]{1,10}$")
_MIME_SUFFIXES = {
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
    "image/x-emf": ".emf",
    "image/x-wmf": ".wmf",
}


def normalize_text(value: str) -> str:
    """Normalize Unicode/newlines while removing unsafe control characters."""

    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    return "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or unicodedata.category(character) != "Cc"
    )


def normalize_media_type(value: str | None, filename: str | None = None) -> str:
    """Return a stable media type for an extracted asset."""

    if value:
        return value.lower().split(";", maxsplit=1)[0].strip()
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


class AssetCollector:
    """Content-addressed, bounded, insertion-ordered asset collection."""

    __slots__ = ("_by_hash", "_options", "_source_name", "_total_bytes")

    def __init__(self, options: ParseOptions, source_name: str) -> None:
        self._options = options
        self._source_name = source_name
        self._by_hash: dict[str, AssetPayload] = {}
        self._total_bytes = 0

    def add(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        media_type: str | None = None,
    ) -> AssetRef:
        """Add one unique payload or reuse an identical existing asset."""

        digest = hashlib.sha256(data).hexdigest()
        existing = self._by_hash.get(digest)
        if existing is not None:
            return existing.ref
        if len(data) > self._options.max_asset_bytes:
            raise UnsafeDocumentError(
                "asset exceeds max_asset_bytes", source_name=self._source_name
            )
        if len(self._by_hash) >= self._options.max_assets:
            raise UnsafeDocumentError(
                "document contains too many assets", source_name=self._source_name
            )
        if self._total_bytes + len(data) > self._options.max_total_asset_bytes:
            raise UnsafeDocumentError(
                "document assets exceed max_total_asset_bytes", source_name=self._source_name
            )

        resolved_type = normalize_media_type(media_type, filename)
        suffix = PurePosixPath((filename or "").replace("\\", "/")).suffix.lower()
        if _SAFE_SUFFIX.fullmatch(suffix) is None:
            suffix = _MIME_SUFFIXES.get(resolved_type, ".bin")
        ref = AssetRef(
            asset_id=f"asset:sha256:{digest}",
            filename=f"sha256-{digest}{suffix}",
            media_type=resolved_type,
            sha256=digest,
            size_bytes=len(data),
        )
        payload = AssetPayload(ref=ref, data=data)
        self._by_hash[digest] = payload
        self._total_bytes += len(data)
        return ref

    @property
    def payloads(self) -> tuple[AssetPayload, ...]:
        """Payloads in deterministic discovery order."""

        return tuple(self._by_hash.values())

    @property
    def refs(self) -> tuple[AssetRef, ...]:
        """Manifest entries in the same order as payloads."""

        return tuple(payload.ref for payload in self._by_hash.values())


def joined_text(values: Iterable[str]) -> str:
    """Join non-empty text fragments with one space."""

    return " ".join(value.strip() for value in values if value.strip())
