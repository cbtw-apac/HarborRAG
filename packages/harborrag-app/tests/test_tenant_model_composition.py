"""Per-tenant chat catalogs are wired only when the deployment asked for them.

Default off, because turning it on moves chat credentials out of the process
environment and gives every configured tenant its own client. A deployment
that switches it on without the ports wired keeps working -- shared catalog,
one warning -- rather than failing to start.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRetrievalFacade, FakeRuntime

from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.resources import AppResources
from harborrag_app.workflow_control.composition.tenant_models import tenant_model_sources
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig

_LOGGER = "harborrag.app.workflow_control.composition.tenant_models"


class _Catalog:
    async def fingerprint(self, tenant_id: str) -> str:
        del tenant_id
        return "fp-1"

    async def chat_catalog(self, tenant_id: str) -> object:
        del tenant_id
        raise AssertionError("not reached")


class _Secrets:
    async def put(self, value: str, *, tenant_id: str) -> str:
        del value, tenant_id
        return "ref-1"

    async def resolve(self, ref: str, *, tenant_id: str) -> str:
        del ref, tenant_id
        return "key"

    async def delete(self, ref: str, *, tenant_id: str) -> None:
        del ref, tenant_id


def _composition(*, catalog: object | None, secrets: object | None) -> SimpleNamespace:
    return SimpleNamespace(control_plane=SimpleNamespace(model_catalog=catalog, secrets=secrets))


def test_the_default_deployment_keeps_the_process_wide_catalog() -> None:
    composition = _composition(catalog=_Catalog(), secrets=_Secrets())

    assert tenant_model_sources(composition, RuntimeSettings()) is None


def test_enabling_it_wires_the_catalog_and_the_secret_store() -> None:
    catalog, secrets = _Catalog(), _Secrets()
    sources = tenant_model_sources(
        _composition(catalog=catalog, secrets=secrets),
        RuntimeSettings(chat_tenant_catalogs_enabled=True),
    )

    assert sources is not None
    assert sources.catalog is catalog
    assert sources.secrets is secrets


def test_enabling_it_without_a_catalog_warns_and_stays_shared(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        sources = tenant_model_sources(
            _composition(catalog=None, secrets=_Secrets()),
            RuntimeSettings(chat_tenant_catalogs_enabled=True),
        )

    assert sources is None
    assert "process-wide chat catalog" in caplog.text


def test_a_composition_with_no_control_plane_stays_shared() -> None:
    settings = RuntimeSettings(chat_tenant_catalogs_enabled=True)

    assert tenant_model_sources(SimpleNamespace(), settings) is None


def test_the_sdk_is_told_about_tenant_catalogs_when_they_are_wired() -> None:
    configured: list[object] = []
    runtime = FakeRuntime(FakeChatFacade(), FakeRetrievalFacade())
    runtime.configure_tenant_models = configured.append  # type: ignore[attr-defined]
    settings = RuntimeSettings(chat_tenant_catalogs_enabled=True)
    resources = AppResources(
        settings,
        runtime_config=TemporalRuntimeConfig.from_settings(settings),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
        composition=_composition(catalog=_Catalog(), secrets=_Secrets()),
    )

    assert resources.runtime_sdk() is runtime
    assert resources.runtime_sdk() is runtime, "the SDK is built at most once"
    assert len(configured) == 1, "and told about tenant catalogs exactly once"


def test_the_sdk_is_left_alone_when_no_catalogs_are_wired() -> None:
    runtime = FakeRuntime(FakeChatFacade(), FakeRetrievalFacade())
    settings = RuntimeSettings()
    resources = AppResources(
        settings,
        runtime_config=TemporalRuntimeConfig.from_settings(settings),
        factories=AppServiceFactories(
            retrieval_runtime=lambda _settings: runtime,  # type: ignore[arg-type]
        ),
        composition=_composition(catalog=_Catalog(), secrets=_Secrets()),
    )

    # No ``configure_tenant_models`` on the double at all: this must not be
    # reached for a deployment that never opted in.
    assert resources.runtime_sdk() is runtime
