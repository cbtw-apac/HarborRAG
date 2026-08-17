"""Deterministic knowledge-graph projection building."""

from .edge_signatures import PROJECTED_EDGE_SIGNATURES
from .graph import GraphProjectionBuilder
from .graph_models import (
    GraphDocumentTarget,
    GraphProjectionBatch,
    GraphProjectionInput,
    UnresolvedGraphRelation,
)
from .source_projectors import (
    ConfluenceSourceProjector,
    GenericSourceProjector,
    GitHubSourceProjector,
    GraphSourceProjector,
    GraphSourceProjectorRegistry,
    JiraSourceProjector,
    LocalSourceProjector,
    SharePointSourceProjector,
    default_graph_source_projector_registry,
)

__all__ = [
    "PROJECTED_EDGE_SIGNATURES",
    "GraphDocumentTarget",
    "GraphProjectionBatch",
    "GraphProjectionBuilder",
    "GraphProjectionInput",
    "GraphSourceProjector",
    "GraphSourceProjectorRegistry",
    "ConfluenceSourceProjector",
    "GenericSourceProjector",
    "GitHubSourceProjector",
    "JiraSourceProjector",
    "LocalSourceProjector",
    "SharePointSourceProjector",
    "UnresolvedGraphRelation",
    "default_graph_source_projector_registry",
]
