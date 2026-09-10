"""Resolve the control-plane ports per-tenant chat models need, or nothing.

Off unless the deployment asked for it. Turning it on moves chat credentials
from the process environment into the encrypted control-plane store and gives
every configured tenant its own client, so it is an explicit choice rather
than something a schema migration switches on underneath a running
deployment. Wired the way ``memories`` and ``model_usage`` are: read off the
control plane if it is there, warn and carry on if it is not.
"""

from __future__ import annotations

import logging
from typing import Any

from harborrag_core.ports.model_catalog import TenantModelCatalogPort
from harborrag_core.ports.secrets import SecretsPort
from harborrag_runtime.chat import TenantModelSources
from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.app.workflow_control.composition.tenant_models")


def tenant_model_sources(
    composition: Any,
    settings: RuntimeSettings,
) -> TenantModelSources | None:
    """The catalog and secrets ports for per-tenant chat, or None to stay shared.

    ``None`` is the default and means every tenant keeps answering from the
    process-wide ``config/models.yaml`` catalog, exactly as before.
    """

    if not settings.chat_tenant_catalogs_enabled:
        return None
    control_plane = getattr(composition, "control_plane", None)
    catalog: TenantModelCatalogPort | None = getattr(control_plane, "model_catalog", None)
    secrets: SecretsPort | None = getattr(control_plane, "secrets", None)
    if catalog is None or secrets is None:
        logger.warning(
            "HARBORRAG_CHAT_TENANT_CATALOGS_ENABLED is set but no model catalog and "
            "secret store are wired on the control plane; every tenant will keep "
            "answering from the process-wide chat catalog."
        )
        return None
    return TenantModelSources(catalog=catalog, secrets=secrets)


__all__ = ["tenant_model_sources"]
