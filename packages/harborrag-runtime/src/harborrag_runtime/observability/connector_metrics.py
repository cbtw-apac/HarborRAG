"""Low-cardinality connector labels for ingestion metrics."""

from harborrag_adapters.connectors import connector_registry
from harborrag_adapters.connectors.exceptions import ConnectorNotFoundError


def connector_metric_label(connector_type: str) -> str:
    """Resolve registered providers and collapse unknown values to ``other``."""

    normalized = connector_type.strip().lower()
    try:
        return connector_registry.canonical_name(normalized)
    except ConnectorNotFoundError:
        return "other"


def connector_metric_labels() -> list[str]:
    """Every value ``connector_metric_label`` can return."""

    return [*connector_registry.canonical_names(), "other"]
