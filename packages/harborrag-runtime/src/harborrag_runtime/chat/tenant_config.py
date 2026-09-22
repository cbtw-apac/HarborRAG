"""Project a tenant's stored chat catalog onto a validated client configuration.

The tenant owns its logical models and their deployments; the operator still
owns everything else. So the shared ``config/models.yaml`` document is loaded
and only its ``chat.default_model`` and ``chat.models`` are replaced. Timeouts,
retry and routing policy, response caching, telemetry privacy, and -- most
importantly -- ``chat.security`` stay exactly as the deployment set them, which
is what keeps a tenant from naming a provider or an endpoint the operator has
not allowed.

API keys never arrive as values. Each deployment carries an opaque
``secret_ref``, which is resolved through ``SecretsPort`` (tenant-scoped) and
handed to the config loader as a ``secret_resolver``, so the key lands in the
configuration as a ``SecretStr`` the sanitizers already redact rather than as a
plain string.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harborrag_core.ports.model_catalog import TenantChatCatalog, TenantModelDeployment
from harborrag_core.ports.secrets import SecretsPort

if TYPE_CHECKING:
    from harborrag_adapters.models.chat import HarborChatClientConfig
    from harborrag_adapters.models.runtime import SecretReference

# A stored row's ``extra`` is a free-form passthrough, so it is sorted here
# rather than trusted whole. These are the deployment fields a tenant may set
# directly.
_DEPLOYMENT_EXTRA_FIELDS = frozenset(
    {
        "api_version",
        "deployment_name",
        "enabled",
        "max_parallel_requests",
        "order",
        "rpm",
        "tpm",
    }
)
# Recognized and deliberately dropped: routing strategy is one client-wide
# policy the operator owns, not something one deployment can switch.
_ROUTING_ONLY_EXTRA_FIELDS = frozenset({"routing_strategy"})
# Credentials, transport, and provider identity a tenant must never set from a
# stored row. Refused by name so the operator's log says which one it was;
# anything else unrecognized becomes an extension parameter, which the
# operator's own ``chat.security.allowed_extra_litellm_params`` still gates.
_FORBIDDEN_EXTRA_FIELDS = frozenset(
    {
        "allow_ambient_credentials",
        "api_base",
        "api_key",
        "aws_access_key_id",
        "aws_role_name",
        "aws_role_session_name",
        "aws_secret_access_key",
        "aws_session_token",
        "capabilities",
        "custom_llm_provider",
        "headers",
        "model",
        "name",
        "provider",
        "vertex_credentials",
        "vertex_location",
        "vertex_project",
        "weight",
    }
)
_SECRET_URI_PREFIX = "secret://harborrag-tenant/"


class TenantChatConfigError(Exception):
    """A tenant's stored configuration cannot be projected onto a client config."""


class _MappedSecretResolver:
    """Resolve only the placeholder URIs this module minted, from memory.

    ``SecretResolver`` is synchronous and ``SecretsPort`` is not, so every ref
    is resolved up front and this resolver just looks the plaintext up. It
    refuses anything it did not mint, so a ``secret://`` string that reached
    the document from anywhere else cannot be resolved by accident.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def resolve(self, reference: SecretReference) -> str:
        value = self._values.get(reference.uri)
        if value is None:
            raise TenantChatConfigError("unresolved tenant secret reference")
        return value


def _reject_expansion(value: str, *, field: str, deployment: str) -> str:
    """Refuse a tenant string that would be expanded from the process environment.

    ``from_dict`` runs ``expand_environment`` over whatever it is given, so a
    stored ``${VAR}`` would otherwise read a process variable the tenant was
    never meant to see.
    """

    if "${" in value:
        raise TenantChatConfigError(
            f"deployment {deployment!r} field {field!r} must not contain an environment reference"
        )
    return value


def _sorted_extra(deployment: TenantModelDeployment) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a stored ``extra`` into deployment fields and extension parameters.

    Returns (fields, extension params). Anything the deployment contract does
    not name becomes an extension parameter rather than being silently
    dropped, so the operator's extension allowlist -- not this function's
    knowledge of LiteLLM -- decides whether it is allowed.
    """

    forbidden = set(deployment.extra) & _FORBIDDEN_EXTRA_FIELDS
    if forbidden:
        raise TenantChatConfigError(
            f"deployment {deployment.name!r} may not set: {sorted(forbidden)}"
        )
    fields: dict[str, Any] = {}
    extension: dict[str, Any] = {}
    for key, value in deployment.extra.items():
        if key in _ROUTING_ONLY_EXTRA_FIELDS:
            continue
        target = fields if key in _DEPLOYMENT_EXTRA_FIELDS else extension
        target[key] = value
    return fields, extension


def _deployment_document(
    deployment: TenantModelDeployment,
    *,
    secret_uri: str | None,
) -> dict[str, Any]:
    fields, extension = _sorted_extra(deployment)
    document: dict[str, Any] = {
        "name": _reject_expansion(deployment.name, field="name", deployment=deployment.name),
        "provider": _reject_expansion(
            deployment.provider, field="provider", deployment=deployment.name
        ),
        "model": _reject_expansion(deployment.model, field="model", deployment=deployment.name),
        "weight": float(deployment.weight),
        "capabilities": dict(deployment.capabilities),
        **fields,
    }
    if extension:
        document["extra_litellm_params"] = extension
    if deployment.api_base is not None:
        document["api_base"] = _reject_expansion(
            deployment.api_base, field="api_base", deployment=deployment.name
        )
    if secret_uri is not None:
        document["api_key"] = secret_uri
    return document


async def _resolved_secrets(
    catalog: TenantChatCatalog,
    secrets: SecretsPort,
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve every distinct ref once, returning (ref -> uri, uri -> plaintext)."""

    uris: dict[str, str] = {}
    values: dict[str, str] = {}
    for definition in catalog.models:
        for deployment in definition.deployments:
            ref = deployment.secret_ref
            if ref is None or ref in uris:
                continue
            uri = f"{_SECRET_URI_PREFIX}{len(uris)}"
            uris[ref] = uri
            values[uri] = await secrets.resolve(ref, tenant_id=catalog.tenant_id)
    return uris, values


async def build_tenant_chat_config(
    catalog: TenantChatCatalog,
    *,
    secrets: SecretsPort,
    shared_config_path: Path,
) -> HarborChatClientConfig:
    """Build one tenant's validated chat configuration, or raise.

    Raises ``TenantChatConfigError`` for a stored configuration this process
    refuses, and whatever ``SecretsPort`` raises for a ref it cannot resolve.
    Either way the caller degrades that tenant to the process-wide client.
    """

    # Imported here, not at module scope: the CLI's Temporal submission path
    # imports this package and must not pull in the model provider runtime.
    from harborrag_adapters.models.chat import HarborChatClientConfig
    from harborrag_adapters.models.runtime import load_config_document

    if catalog.is_empty():
        raise TenantChatConfigError(f"tenant {catalog.tenant_id!r} configured no chat models")
    uris, values = await _resolved_secrets(catalog, secrets)
    models: dict[str, Any] = {}
    for definition in catalog.models:
        models[definition.logical_model] = {
            "deployments": [
                _deployment_document(
                    deployment,
                    secret_uri=(
                        None if deployment.secret_ref is None else uris[deployment.secret_ref]
                    ),
                )
                for deployment in definition.deployments
            ]
        }
    document = load_config_document(shared_config_path)
    shared = document.get("chat")
    section: dict[str, Any] = dict(shared) if isinstance(shared, Mapping) else {}
    section["models"] = models
    section["default_model"] = catalog.default_model or next(iter(models))
    try:
        return HarborChatClientConfig.from_dict(
            {"chat": section},
            secret_resolver=_MappedSecretResolver(values),
        )
    except TenantChatConfigError:
        raise
    except Exception as exc:
        raise TenantChatConfigError(str(exc)) from exc


__all__ = ["TenantChatConfigError", "build_tenant_chat_config"]
