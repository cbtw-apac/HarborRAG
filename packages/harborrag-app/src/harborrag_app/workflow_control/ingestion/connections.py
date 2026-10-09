"""Read-only view of the connections a caller may submit ingestion for.

``POST /v1/ingestions`` selects a preconfigured connection by its
``config/connectors.yaml`` key, which no HTTP client can read. This exposes
just that identity -- name and provider -- so a UI can offer the same choices
the worker will accept. Provider settings, environment references, and secret
references deliberately stay server-side.
"""

from __future__ import annotations

import logging

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_runtime.config import load_connector_catalog
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.app.workflow_control.ingestion.connections")


def connection_catalog(
    settings: RuntimeSettings, *, tenant_ids: frozenset[str] | None = None
) -> dict[str, object]:
    """List the enabled connections a caller may use, alphabetically, with their type.

    ``tenant_ids`` is the caller's tenant scope (None: unrestricted). A
    connection with a ``tenants`` allowlist is listed only to callers in one
    of those tenants; one without an allowlist is listed to everyone.

    The catalog is re-read on every call, so a connection enabled in the YAML
    after process start becomes selectable without an API restart. A
    definition that fails to parse is omitted rather than failing the whole
    listing -- it is already unusable for submission. An unreadable or
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
            for name in catalog.names_for_tenants(tenant_ids)
        ]
    }


def warn_unbound_connections(settings: RuntimeSettings) -> list[str]:
    """Log (once, at startup) enabled connections open to every tenant.

    Only meaningful once some connection declares a ``tenants`` allowlist;
    see ConnectorCatalog.unbound_connection_names. Never fails startup: an
    unreadable catalog is reported by the routes that need it.
    """

    try:
        catalog = load_connector_catalog(settings.connector_config_path)
    except ConnectorConfigurationError:
        return []
    unbound = catalog.unbound_connection_names()
    if unbound:
        logger.warning(
            "Connections without a tenants allowlist are usable by every tenant: %s. "
            "Add tenants: [...] to each, or confirm they are meant to be shared.",
            ", ".join(unbound),
        )
    return unbound


__all__ = ["connection_catalog", "warn_unbound_connections"]
