"""Validated adapter and conversion result bundles."""

from __future__ import annotations

import hashlib

from pydantic import ConfigDict, Field, model_validator

from document_parser.models import AssetRef, Document, FrozenModel


class AssetPayload(FrozenModel):
    """Immutable binary content matching one asset manifest entry."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    ref: AssetRef
    data: bytes = Field(repr=False)

    @model_validator(mode="after")
    def validate_payload(self) -> AssetPayload:
        if len(self.data) != self.ref.size_bytes:
            raise ValueError("asset payload size does not match its manifest")
        if hashlib.sha256(self.data).hexdigest() != self.ref.sha256:
            raise ValueError("asset payload hash does not match its manifest")
        return self


class AdapterOutput(FrozenModel):
    """Lossless output produced by a format adapter."""

    document: Document
    assets: tuple[AssetPayload, ...] = ()

    @model_validator(mode="after")
    def validate_assets(self) -> AdapterOutput:
        manifest = {asset.asset_id: asset for asset in self.document.assets}
        payloads = {payload.ref.asset_id: payload.ref for payload in self.assets}
        if len(payloads) != len(self.assets):
            raise ValueError("asset payload IDs must be unique")
        if manifest != payloads:
            raise ValueError("asset payloads must exactly match the document manifest")
        return self


class ConversionResult(AdapterOutput):
    """Document IR, canonical Markdown, and extracted binary assets."""

    markdown: str
