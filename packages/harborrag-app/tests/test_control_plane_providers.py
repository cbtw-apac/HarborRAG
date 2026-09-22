"""Direct tests of the provider CRUD/test-connection/routing/cost use cases.

Exercises ``ControlPlaneReadsMixin``/``ControlPlaneWritesMixin`` (the real
production logic) against fake repositories -- not the HTTP layer -- so the
secret-handling and probe-boundary invariants are proven at the source, not
just observed through a mocked API contract test.
"""

from __future__ import annotations

import pytest
from app_test_control_plane import control_plane_app_service

from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.routing_rule import RoutingRule
from harborrag_core.ports.provider_probe import ProviderProbeResult
from harborrag_core.testing.control_plane_fakes import FakeProviderProbe
from harborrag_core.testing.fakes import FakeSecrets


@pytest.mark.asyncio
async def test_create_provider_stores_a_secret_ref_never_the_raw_key() -> None:
    service = control_plane_app_service()

    response = await service.create_provider(
        tenant_id="ACME",
        name="OpenAI",
        family="chat",
        config={"model": "openai/gpt-4o-mini"},
        api_key="sk-super-secret",
        actor="alice",
    )

    provider = response.data["provider"]
    assert provider.secret_ref is not None
    assert provider.secret_ref.startswith("secret://")
    assert "sk-super-secret" not in repr(provider)
    control_plane = service._control_plane()
    assert (
        await control_plane.secrets.resolve(provider.secret_ref, tenant_id="ACME")
        == "sk-super-secret"
    )
    activity = await control_plane.activity.list(tenant_ids=None)
    assert activity[0].verb == "created"
    assert activity[0].entity_type == "provider"


@pytest.mark.asyncio
async def test_create_provider_without_an_api_key_leaves_secret_ref_none() -> None:
    service = control_plane_app_service()

    response = await service.create_provider(
        tenant_id="ACME",
        name="Local",
        family="chat",
        config={"model": "ollama/llama3"},
        api_key=None,
        actor="alice",
    )

    assert response.data["provider"].secret_ref is None


@pytest.mark.asyncio
async def test_create_provider_rejects_a_malformed_tenant_id_without_leaking_a_secret() -> None:
    """A tenant_id with whitespace used to fail domain validation with a bare
    ValueError, which the API's generic exception handler turned into an
    opaque 500 -- and by then the api_key had already been written to the
    secrets port with nothing left to retire it. Must be a clean 422 instead,
    raised before the secret is ever stored."""
    secrets = FakeSecrets()
    service = control_plane_app_service(secrets=secrets)

    with pytest.raises(HarborValidationError):
        await service.create_provider(
            tenant_id="  bad tenant  ",
            name="OpenAI",
            family="chat",
            config={"model": "openai/gpt-4o-mini"},
            api_key="sk-super-secret",
            actor="alice",
        )

    assert secrets.values == {}


@pytest.mark.asyncio
async def test_update_provider_rotates_the_secret_and_retires_the_old_one() -> None:
    secrets = FakeSecrets()
    old_ref = await secrets.put("sk-old-value", tenant_id="ACME")
    provider = Provider(
        id="prov_1", tenant_id="ACME", name="OpenAI", family="chat", secret_ref=old_ref
    )
    service = control_plane_app_service(providers=[provider], secrets=secrets)

    response = await service.update_provider(
        "prov_1",
        updates={"api_key": "sk-new-value"},
        actor="bob",
        tenant_ids=frozenset({"ACME"}),
    )

    updated = response.data["provider"]
    assert updated.secret_ref != old_ref
    assert await secrets.resolve(updated.secret_ref, tenant_id="ACME") == "sk-new-value"
    with pytest.raises(KeyError):
        await secrets.resolve(old_ref, tenant_id="ACME")


@pytest.mark.asyncio
async def test_update_provider_rejects_an_unsupported_field() -> None:
    provider = Provider(id="prov_1", tenant_id="ACME", name="OpenAI", family="chat")
    service = control_plane_app_service(providers=[provider])

    with pytest.raises(HarborValidationError):
        await service.update_provider(
            "prov_1",
            updates={"family": "embedding"},
            actor="bob",
            tenant_ids=frozenset({"ACME"}),
        )


@pytest.mark.asyncio
async def test_update_provider_rejects_a_secret_shaped_config_value() -> None:
    provider = Provider(id="prov_1", tenant_id="ACME", name="OpenAI", family="chat")
    service = control_plane_app_service(providers=[provider])

    with pytest.raises(HarborValidationError, match="secret reference"):
        await service.update_provider(
            "prov_1",
            updates={"config": {"api_key": "raw-value-should-be-rejected"}},
            actor="bob",
            tenant_ids=frozenset({"ACME"}),
        )


@pytest.mark.asyncio
async def test_get_and_delete_provider_outside_tenant_scope_is_not_found() -> None:
    provider = Provider(id="prov_1", tenant_id="ACME", name="OpenAI", family="chat")
    service = control_plane_app_service(providers=[provider])

    with pytest.raises(HarborNotFoundError):
        await service.get_provider("prov_1", tenant_ids=frozenset({"other-tenant"}))
    with pytest.raises(HarborNotFoundError):
        await service.delete_provider("prov_1", actor="bob", tenant_ids=frozenset({"other-tenant"}))


@pytest.mark.asyncio
async def test_delete_provider_retires_its_secret() -> None:
    service = control_plane_app_service()
    control_plane = service._control_plane()
    ref = await control_plane.secrets.put("sk-value", tenant_id="ACME")
    provider = Provider(id="prov_1", tenant_id="ACME", name="OpenAI", family="chat", secret_ref=ref)
    await control_plane.providers.save(provider)

    await service.delete_provider("prov_1", actor="bob", tenant_ids=frozenset({"ACME"}))

    assert await control_plane.providers.get("prov_1", tenant_ids=None) is None
    with pytest.raises(KeyError):
        await control_plane.secrets.resolve(ref, tenant_id="ACME")


@pytest.mark.asyncio
async def test_test_provider_connection_calls_the_probe_for_chat_providers() -> None:
    probe = FakeProviderProbe(result=ProviderProbeResult(ok=True, message="ok", latency_ms=5.0))
    provider = Provider(id="prov_1", tenant_id="ACME", name="OpenAI", family="chat")
    service = control_plane_app_service(providers=[provider], provider_probe=probe)

    response = await service.test_provider_connection("prov_1", tenant_ids=frozenset({"ACME"}))

    assert response.data["result"].ok is True
    assert probe.probed == [provider]


@pytest.mark.asyncio
async def test_test_provider_connection_never_probes_a_non_chat_family() -> None:
    probe = FakeProviderProbe()
    provider = Provider(id="prov_1", tenant_id="ACME", name="Embedder", family="embedding")
    service = control_plane_app_service(providers=[provider], provider_probe=probe)

    with pytest.raises(HarborCapabilityError):
        await service.test_provider_connection("prov_1", tenant_ids=frozenset({"ACME"}))

    assert probe.probed == []


@pytest.mark.asyncio
async def test_replace_routing_rules_rejects_a_reference_to_a_missing_provider() -> None:
    service = control_plane_app_service()

    with pytest.raises(HarborNotFoundError):
        await service.replace_routing_rules(
            [{"family": "chat", "provider_id": "does-not-exist", "priority": 0}],
            actor="bob",
        )


@pytest.mark.asyncio
async def test_replace_routing_rules_rejects_a_non_numeric_priority() -> None:
    """A malformed ``priority`` used to raise a bare ValueError from ``int(...)``,
    which surfaced as an opaque 500 instead of a clean validation error."""
    provider = Provider(id="prov_1", tenant_id="ACME", name="OpenAI", family="chat")
    service = control_plane_app_service(providers=[provider])

    with pytest.raises(HarborValidationError):
        await service.replace_routing_rules(
            [{"family": "chat", "provider_id": "prov_1", "priority": "not-a-number"}],
            actor="bob",
        )


@pytest.mark.asyncio
async def test_replace_routing_rules_atomically_swaps_the_table() -> None:
    provider = Provider(id="prov_1", tenant_id="ACME", name="OpenAI", family="chat")
    stale_rule = RoutingRule(id="rule_old", family="chat", provider_id="prov_1", priority=9)
    service = control_plane_app_service(providers=[provider], routing_rules=[stale_rule])

    response = await service.replace_routing_rules(
        [{"family": "chat", "provider_id": "prov_1", "priority": 1}],
        actor="bob",
    )

    rules = response.data["rules"]
    assert len(rules) == 1
    assert rules[0].priority == 1
    listed = (await service.list_routing_rules()).data["rules"]
    assert [rule.id for rule in listed] == [rules[0].id]


@pytest.mark.asyncio
async def test_provider_cost_snapshot_is_zero_for_a_brand_new_workspace() -> None:
    provider = Provider(id="prov_1", tenant_id="ACME", name="OpenAI", family="chat")
    service = control_plane_app_service(providers=[provider])

    response = await service.get_provider_cost(tenant_ids=frozenset({"ACME"}))

    assert response.data["providers"] == {"prov_1": 0.0}
    assert response.data["since"] is not None
