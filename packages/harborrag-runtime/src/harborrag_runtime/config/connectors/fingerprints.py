from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256

from harborrag_adapters.connectors import connector_registry
from harborrag_runtime.config.connectors.schemas import ConnectorDefinition
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.serialization import to_json_value


def connector_fingerprint(
    *,
    catalog_version: int,
    definition: ConnectorDefinition,
    environment: Mapping[str, str],
) -> str:
    """Fingerprint the non-secret inputs that select connector behavior."""

    path_fields = frozenset(
        connector_registry.get_definition(definition.provider).config_path_fields
    )
    setting_environment: dict[str, object] = {}
    for field_name, variable_name in definition.setting_environment.items():
        value = environment.get(variable_name, "")
        if not value:
            raise ConnectorConfigurationError(
                f"Connector {definition.name!r} requires environment variable "
                f"{variable_name!r} for {field_name!r}"
            )
        # A host CLI and its container worker see the same mounted directory at
        # different physical paths (for example ``docs`` and ``/data/sources``).
        # The environment reference identifies that deployment input; hashing the
        # machine-local path makes otherwise identical submitter/worker configs
        # disagree. Literal YAML paths remain fingerprinted in ``settings``.
        setting_environment[field_name] = (
            {"environment": variable_name} if field_name in path_fields else value
        )
    payload = {
        "catalog_version": catalog_version,
        "name": definition.name,
        "provider": definition.provider,
        "settings": to_json_value(definition.settings),
        "setting_environment": setting_environment,
        "secret_references": dict(definition.secret_environment),
    }
    encoded = json.dumps(
        to_json_value(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"connector-{sha256(encoded).hexdigest()[:32]}"
