"""Control-plane ports for per-tenant chat models, resolved only when asked for.

Off unless the deployment asked for it. Turning it on moves chat credentials
from the process environment into the encrypted control-plane store and gives
every configured tenant its own client, so it is an explicit choice rather
than something a schema migration switches on underneath a running
deployment. Wired the way ``memories`` and ``model_usage`` are: read off the
control plane if it is there, warn and carry on if it is not.
"""

from __future__ import annotations

import logging

from harborrag_core.observability.log_events import log_event
from harborrag_core.ports.model_catalog import TenantModelCatalogPort
from harborrag_core.ports.secrets import SecretsPort
from harborrag_runtime.chat import TenantModelSources
from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.application.composition.tenant_models")


def tenant_model_sources(
    composition: object,
    settings: RuntimeSettings,
) -> TenantModelSources | None:
    """Return the catalog and secrets ports for per-tenant chat, or None to stay shared.

    ``None`` is the default and means every tenant keeps answering from the
    process-wide ``config/models/models.yaml`` catalog, exactly as before.

    Args:
        composition: The composition root; its ``control_plane`` holds the ports.
        settings: Runtime settings; per-tenant catalogs are off unless they turn them on.

    Returns:
        The model catalog and secrets ports, or None when catalogs are off or not wired.
    """
    if not settings.chat_tenant_catalogs_enabled:
        return None
    control_plane = getattr(composition, "control_plane", None)
    catalog: TenantModelCatalogPort | None = getattr(control_plane, "model_catalog", None)
    secrets: SecretsPort | None = getattr(control_plane, "secrets", None)
    if catalog is None or secrets is None:
        log_event(
            logger,
            "composition.tenant_model_catalogs_unwired",
            level=logging.WARNING,
            hint=(
                "Run with the control-plane database (HARBORRAG_CONTROL_DB_URL), or unset "
                "HARBORRAG_CHAT_TENANT_CATALOGS_ENABLED; until then every tenant answers "
                "from the process-wide chat catalog."
            ),
        )
        return None
    return TenantModelSources(catalog=catalog, secrets=secrets)
