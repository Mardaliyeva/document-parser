"""Tests for installed distribution metadata and typing marker."""

from importlib import resources
from importlib.metadata import metadata, version

import document_parser


def test_distribution_metadata_matches_package() -> None:
    package_metadata = metadata("document-parser")

    assert package_metadata["Name"] == "document-parser"
    assert set(package_metadata["Requires-Python"].split(",")) == {">=3.11", "<3.13"}
    assert package_metadata["License-Expression"] == "Apache-2.0"
    assert version("document-parser") == document_parser.__version__
    requirements = package_metadata.get_all("Requires-Dist") or []
    assert any(requirement.startswith("pydantic") for requirement in requirements)


def test_typing_marker_is_packaged() -> None:
    marker = resources.files("document_parser").joinpath("py.typed")
    assert marker.is_file()
