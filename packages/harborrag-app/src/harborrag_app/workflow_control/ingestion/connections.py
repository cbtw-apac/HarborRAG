"""Read-only view of the connections a caller may submit ingestion for.

``POST /v1/ingestions`` selects a preconfigured connection by its
``config/connectors.yaml`` key, which no HTTP client can read. This exposes
just that identity -- name and provider -- so a UI can offer the same choices
the worker will accept. Provider settings, environment references, and secret
references deliberately stay server-side.
"""

from __future__ import annotations

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_runtime.config import load_connector_catalog
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings


def connection_catalog(settings: RuntimeSettings) -> dict[str, object]:
    """List enabled connections alphabetically with their provider type.

    The catalog is re-read on every call, so a connection enabled in the YAML
    after process start becomes selectable without an API restart. A
    definition that fails to parse is omitted rather than failing the whole
    listing -- it is already unusable for submission, and its error surfaces
    when that specific connection is submitted. An unreadable or
    version-invalid file is a deployment fault, so it fails the request.
    """

    try:
        catalog = load_connector_catalog(settings.connector_config_path)
    except ConnectorConfigurationError as error:
        raise HarborConfigurationError("Connector configuration is unavailable.") from error
    return {
        "items": [
            {
                "connection_id": name,
                "source_type": catalog.connectors[name].provider,
            }
            for name in catalog.names(enabled_only=True)
        ]
    }


__all__ = ["connection_catalog"]
