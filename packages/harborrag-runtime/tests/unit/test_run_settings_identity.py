from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from harborrag_core.config_store import SettingsLevel, SettingsSnapshot, resolve_effective
from harborrag_runtime.config import load_parser_catalog
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config_store.defaults import registry_for
from harborrag_runtime.config_store.run_settings import RunSettingsResolver
from harborrag_runtime.ingestion.profiles import build_processing_profile
from harborrag_runtime.ingestion_contracts import RunSettings

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_PARSER_PROFILE = "parser-f325dde1d1ed01ca"
GOLDEN_CHUNK_STRATEGY = (
    "chunking-v3-c8b988021028d46534f990f2445e1f3925b7fe79992f592567aa2af0eaddbb31"
)


@pytest.fixture
def shipped_settings() -> RuntimeSettings:
    return RuntimeSettings(
        parser_config_path=REPO_ROOT / "config/ingestion/parsers.yaml",
        chunking_config_path=REPO_ROOT / "config/ingestion/chunking.yaml",
    )


def test_default_run_settings_keep_todays_identity(shipped_settings: RuntimeSettings) -> None:
    assert build_processing_profile(
        shipped_settings, run_settings=RunSettings("rev_0")
    ) == build_processing_profile(shipped_settings)


def test_default_identity_is_pinned(shipped_settings: RuntimeSettings) -> None:
    profile = build_processing_profile(shipped_settings)

    assert profile.parser_profile == GOLDEN_PARSER_PROFILE
    assert profile.chunk_strategy == GOLDEN_CHUNK_STRATEGY


def test_cap_changes_chunk_strategy(shipped_settings: RuntimeSettings) -> None:
    capped = build_processing_profile(
        shipped_settings, run_settings=RunSettings("rev_1", chunk_max_tokens=256)
    )

    assert capped.chunk_strategy != GOLDEN_CHUNK_STRATEGY
    assert capped.parser_profile == GOLDEN_PARSER_PROFILE


def test_cap_above_profiles_keeps_identity(shipped_settings: RuntimeSettings) -> None:
    profile = build_processing_profile(
        shipped_settings, run_settings=RunSettings("rev_1", chunk_max_tokens=1200)
    )

    assert profile.chunk_strategy == GOLDEN_CHUNK_STRATEGY


def test_non_default_pdf_parser_suffixes_parser_profile(shipped_settings: RuntimeSettings) -> None:
    docling = build_processing_profile(
        shipped_settings, run_settings=RunSettings("rev_1", pdf_parser="docling")
    )
    liteparse = build_processing_profile(
        shipped_settings, run_settings=RunSettings("rev_1", pdf_parser="liteparse")
    )

    assert docling.parser_profile == f"{GOLDEN_PARSER_PROFILE}+pdf-docling"
    assert liteparse.parser_profile == GOLDEN_PARSER_PROFILE


def test_run_settings_json_round_trip() -> None:
    settings = RunSettings("rev_3", "docling", 256, 4)

    assert RunSettings.from_json(settings.to_json()) == settings
    assert RunSettings.from_json(RunSettings("rev_0").to_json()) == RunSettings("rev_0")
    assert RunSettings.from_json(None) is None


def test_catalog_pdf_selection(tmp_path: Path) -> None:
    path = tmp_path / "parsers.yaml"
    path.write_text(
        dedent(
            """
            version: 1
            parsers:
              pdf-liteparse:
                parser: pdf
                engines:
                  - backend: liteparse
              pdf-docling:
                parser: pdf
                enabled: false
                engines:
                  - backend: docling
            """
        ),
        encoding="utf-8",
    )

    catalog = load_parser_catalog(path)

    assert catalog.enabled_pdf_backend() == "liteparse"
    assert catalog.pdf_definition_for("docling") == "pdf-docling"
    assert (
        load_parser_catalog(REPO_ROOT / "config/ingestion/parsers.yaml").pdf_definition_for(
            "docling"
        )
        is None
    )


class _FixedProvider:
    def __init__(self, snapshot: SettingsSnapshot) -> None:
        self._snapshot = snapshot

    async def snapshot(self, tenant_id: str) -> SettingsSnapshot:
        return self._snapshot

    async def effective(self, tenant_id: str, level: SettingsLevel):  # noqa: ANN201
        return resolve_effective(registry_for(RuntimeSettings()), self._snapshot, level)

    def invalidate(self, tenant_id: str | None = None) -> None:
        return None


@pytest.mark.asyncio
async def test_resolver_reports_overrides_only() -> None:
    snapshot = SettingsSnapshot("tenant-a", 3, {"connection:p1/c1": {"chunking.max_tokens": 256}})

    run_settings = await RunSettingsResolver(_FixedProvider(snapshot)).for_connection(
        tenant_id="tenant-a", project_id="p1", connection_id="c1"
    )

    assert run_settings == RunSettings("rev_3", None, 256, None)


def test_build_registry_swaps_in_the_selected_pdf_definition(tmp_path: Path) -> None:
    path = tmp_path / "parsers.yaml"
    path.write_text(
        dedent(
            """
            version: 1
            parsers:
              pdf-liteparse:
                parser: pdf
                engines:
                  - backend: liteparse
              pdf-pymupdf:
                parser: pdf
                enabled: false
                engines:
                  - backend: pymupdf
            """
        ),
        encoding="utf-8",
    )
    catalog = load_parser_catalog(path)

    def pdf_backends(registry) -> list[str]:  # noqa: ANN001
        (pdf,) = [family for family in registry.families() if family.parser_name == "pdf"]
        return [backend.name for backend in pdf.backends]

    assert pdf_backends(catalog.build_registry()) == ["liteparse"]
    assert pdf_backends(catalog.build_registry(pdf_definition="pdf-pymupdf")) == ["pymupdf"]
