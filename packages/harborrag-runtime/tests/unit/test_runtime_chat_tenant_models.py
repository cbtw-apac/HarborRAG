"""``RuntimeChatService`` picks the client, and bounds the model, per tenant.

Which client answers is read from ``request.metadata.tenant_id`` -- the same
field the chat surface and the engine agent both already stamp -- so nothing
had to be threaded through the request contract to make this work.
"""

from __future__ import annotations

import pytest
from tenant_chat_fixtures import (
    FakeCatalog,
    FakeSecrets,
    FakeTenantChatClient,
    tenant_catalog,
)

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.models.chat import HarborChatMessage, HarborChatMetadata, HarborChatRequest
from harborrag_runtime.chat import RuntimeChatService, TenantModelSources
from harborrag_runtime.config.settings import RuntimeSettings

_SHARED_DOCUMENT = {
    "chat": {
        "default_model": "primary",
        "models": {
            "primary": {
                "deployments": [
                    {"name": "shared", "provider": "openai", "model": "gpt-4o-mini"},
                ]
            }
        },
    }
}


def _shared_config(_settings: RuntimeSettings) -> HarborChatClientConfig:
    return HarborChatClientConfig.from_dict(_SHARED_DOCUMENT)


def _request(tenant_id: str | None = "acme") -> HarborChatRequest:
    return HarborChatRequest(
        messages=(HarborChatMessage.user("hi"),),
        metadata=HarborChatMetadata(tenant_id=tenant_id),
    )


def _service(
    catalog: FakeCatalog,
    *,
    shared: FakeTenantChatClient | None = None,
    built: list[FakeTenantChatClient] | None = None,
) -> tuple[RuntimeChatService, FakeTenantChatClient, list[FakeTenantChatClient]]:
    process_wide = shared or FakeTenantChatClient("shared")
    made = built if built is not None else []

    def build_tenant(config: HarborChatClientConfig) -> FakeTenantChatClient:
        client = FakeTenantChatClient(config.default_model)
        made.append(client)
        return client

    service = RuntimeChatService(
        RuntimeSettings(),
        client_builder=lambda _settings: process_wide,
        shared_config_loader=_shared_config,
    )
    service.configure_tenant_models(
        TenantModelSources(catalog=catalog, secrets=FakeSecrets()),
        builder=build_tenant,  # type: ignore[arg-type]
    )
    return service, process_wide, made


@pytest.mark.asyncio
async def test_an_unconfigured_tenant_is_answered_by_the_process_wide_client() -> None:
    service, shared, built = _service(FakeCatalog())

    await service.complete(_request())

    assert shared.calls == 1
    assert built == []


@pytest.mark.asyncio
async def test_a_configured_tenant_is_answered_by_its_own_client() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    service, shared, built = _service(catalog)

    await service.complete(_request())

    assert shared.calls == 0
    assert built[0].calls == 1


@pytest.mark.asyncio
async def test_a_request_with_no_tenant_keeps_the_process_wide_client() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    service, shared, built = _service(catalog)

    await service.complete(_request(None))

    assert shared.calls == 1
    assert built == []


@pytest.mark.asyncio
async def test_aclose_disposes_the_shared_and_every_tenant_client() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    service, shared, built = _service(catalog)

    await service.complete(_request())
    await service.complete(_request(None))
    await service.aclose()

    assert shared.closes == 1
    assert [client.closes for client in built] == [1]


@pytest.mark.asyncio
async def test_a_tenant_may_only_name_a_model_its_own_catalog_configured() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    service, _shared, _built = _service(catalog)

    await service.validate_model("tenant-primary", tenant_id="acme")

    with pytest.raises(HarborValidationError) as rejected:
        await service.validate_model("primary", tenant_id="acme")
    assert rejected.value.details == {"field": "model"}


@pytest.mark.asyncio
async def test_a_tenant_without_a_catalog_is_bounded_by_the_process_wide_one() -> None:
    service, _shared, _built = _service(FakeCatalog())

    await service.validate_model("primary", tenant_id="acme")

    with pytest.raises(HarborValidationError) as rejected:
        await service.validate_model("tenant-primary", tenant_id="acme")
    assert rejected.value.details == {"field": "model"}


@pytest.mark.asyncio
async def test_omitting_a_model_never_validates_anything() -> None:
    catalog = FakeCatalog({"acme": tenant_catalog("acme")}, fingerprints={"acme": "fp-1"})
    service, _shared, _built = _service(catalog)

    await service.validate_model(None, tenant_id="acme")

    assert catalog.fingerprint_calls == 0


@pytest.mark.asyncio
async def test_a_deployment_with_no_tenant_catalogs_validates_against_the_shared_one() -> None:
    service = RuntimeChatService(
        RuntimeSettings(),
        client_builder=lambda _settings: FakeTenantChatClient("shared"),
        shared_config_loader=_shared_config,
    )

    await service.validate_model("primary", tenant_id="acme")

    with pytest.raises(HarborValidationError):
        await service.validate_model("nope", tenant_id="acme")
