from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from harborrag_core.ingestion import ProcessingProfile
from harborrag_runtime.config.settings import RuntimeSettings

from .document.normalization import CANONICAL_NORMALIZER_VERSION

# graph-v3: Jira parent/subtask placeholders switched identity from numeric issue id
# to issue key; the bump forces a graph rebuild so id-keyed placeholders and their
# edges written by graph-v2 builds are replaced instead of stranded alongside.
GRAPH_PROJECTION_VERSION = "graph-v3"
VECTOR_PROJECTION_SCHEMA = "vector-v2"


def build_processing_profile(settings: RuntimeSettings) -> ProcessingProfile:
    """Build the deterministic processing identity shared by clients and workers."""

    return ProcessingProfile(
        parser_profile=configuration_file_digest(
            "parser",
            settings.parser_config_path,
        ),
        normalizer_version=CANONICAL_NORMALIZER_VERSION,
        chunk_strategy="canonical-source-policies-route-evidence-v2",
        dense_encoder_profile=settings.dense_encoder_profile,
        sparse_encoder_profile=settings.sparse_encoder_profile,
        graph_projection_version=GRAPH_PROJECTION_VERSION,
        vector_projection_schema=VECTOR_PROJECTION_SCHEMA,
    )


def configuration_file_digest(prefix: str, path: Path) -> str:
    digest = sha256(path.read_bytes()).hexdigest()[:16]
    return f"{prefix}-{digest}"
