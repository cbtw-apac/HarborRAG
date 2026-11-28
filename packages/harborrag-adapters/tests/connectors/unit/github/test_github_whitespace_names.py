"""Regression: GitHub paths that end in whitespace keep their extension."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig
from harborrag_adapters.connectors.github.filters import should_process_file
from harborrag_adapters.connectors.github.repository_paths import (
    file_extension,
    guess_mime_type,
)
from harborrag_core.ingestion.connector_contracts import ConnectorQuery

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "path", ["docs/notes.md ", "docs/notes.md\u00a0", "docs/notes.md\ufeff", "Q3 docs/notes.md"]
)
def test_github_path_with_whitespace_passes_the_extension_filter(path: str) -> None:
    """A space after the extension neither filters the file out nor loses its media type."""
    config = GitHubRepositoryConfig(owner="acme", repo="handbook")
    item = {"path": path, "type": "blob", "size": 12}

    assert should_process_file(config, item, ConnectorQuery(), commit={}) is True
    assert file_extension(path) == ".md"
    assert guess_mime_type(path) == "text/markdown"
