"""Regression: graph file keys and reindex keep the Unicode form of a source item's path."""

from __future__ import annotations

import pytest

from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import identity_for_source
from harborrag_engine.ingestion.maintenance.reindex import source_identity_from_canonical

from .chunking_helpers import make_document
from .test_source_identity_spacing import _file_node_key

pytestmark = pytest.mark.unit

_SPELLINGS = ("docs/caf\u00e9.md", "docs/cafe\u0301.md")


def test_local_files_whose_names_differ_only_in_unicode_form_get_two_file_nodes() -> None:
    keys = {_file_node_key("local", {"relative_path": path}, path) for path in _SPELLINGS}

    assert len(keys) == 2


def test_github_files_whose_names_differ_only_in_unicode_form_get_two_file_nodes() -> None:
    keys = {
        _file_node_key(
            "github", {"path": path, "repo": "handbook"}, f"github://demo/handbook/{path}"
        )
        for path in _SPELLINGS
    }

    assert len(keys) == 2


def test_reindex_recovers_the_decomposed_identity_it_stored() -> None:
    composed, decomposed = (f"github://demo/handbook/{path}" for path in _SPELLINGS)
    canonical = make_document(
        [DocumentElement("p1", "paragraph", "Release checklist")],
        extra={
            "tenant_id": "acme",
            "connector_type": "github",
            "connection_id": "handbook",
            "source_item_id": decomposed,
            "source_scope_id": "scope-handbook",
        },
    )

    identity = source_identity_from_canonical(canonical)

    assert identity.source_item_id == decomposed
    assert identity_for_source(identity) != identity_for_source(
        identity.model_copy(update={"source_item_id": composed})
    )
