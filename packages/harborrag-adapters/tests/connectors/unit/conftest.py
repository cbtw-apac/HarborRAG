"""Run connector output through the real identity and change-detection pipeline.

A connector refactor that changes any of these values costs a full corpus
pass: a new ``document_id`` makes every item new (the old ones retire after
missed scans), a new ``admission_change_key`` re-fetches it, a new
``canonical_content_hash`` re-chunks and re-embeds it, and a new
``retrieval_metadata_hash`` writes a new version of it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_core.chunking import ConnectorType
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import ProcessingProfile

type IdentityPins = dict[str, tuple[str, str, str, str]]
type PinConnector = Callable[..., IdentityPins]

_PROCESSING = ProcessingProfile(
    parser_profile="pin",
    normalizer_version="pin",
    chunk_strategy="pin",
    dense_encoder_profile="pin",
    sparse_encoder_profile="pin",
    graph_projection_version="pin",
)


def _pins(
    connector: BaseConnector,
    *,
    connector_type: ConnectorType,
    records: Iterable[SourceRecord] | None = None,
) -> IdentityPins:
    """Return ``(document_id, admission_change_key, canonical_content_hash,
    retrieval_metadata_hash)`` for every root and bound record, keyed by record id."""
    # Imported here so connector unit tests that never pin do not load engine and runtime.
    from harborrag_adapters.parsers.factory import HarborParserFactory
    from harborrag_engine.ingestion.admission import CanonicalVersionPlanner
    from harborrag_engine.ingestion.title_content import with_title_as_content
    from harborrag_runtime.ingestion.document.normalization import (
        build_source_document_normalizer,
    )
    from harborrag_runtime.ingestion.source.descriptor_mapping import SourceDescriptorMapper
    from harborrag_runtime.ingestion.source.models import SourceIngestionRequest

    request = SourceIngestionRequest(
        tenant_id="tenant-pin",
        task_id="task-pin",
        connector_name=connector_type.value,
        connector_type=connector_type,
        connection_id="connection-pin",
        source_scope_id="scope-pin",
        configuration_fingerprint="configuration-pin",
        processing=_PROCESSING,
    )
    mapper = SourceDescriptorMapper()
    parser = HarborParserFactory().create_registry()
    normalizer = build_source_document_normalizer()
    planner = CanonicalVersionPlanner()
    pins: IdentityPins = {}
    for record in connector.discover() if records is None else records:
        for pending in mapper.releases(request, connector.describe(record)):
            release = pending.release.request
            identity = release.source_identity
            raw = connector.load(release.source)
            document = with_title_as_content(
                normalizer.normalize(raw, parser.parse(raw)),
                binding=identity.binding.kind,
            )
            fingerprints = planner.plan(
                document=document,
                source_identity=identity,
                admission=release.admission,
                processing=_PROCESSING,
            ).candidate.fingerprints
            pins[release.source.id] = (
                str(pending.item.document_id),
                pending.item.admission_change_key,
                fingerprints.canonical_content_hash,
                fingerprints.retrieval_metadata_hash,
            )
    return pins


@pytest.fixture
def pin_connector() -> PinConnector:
    return _pins
