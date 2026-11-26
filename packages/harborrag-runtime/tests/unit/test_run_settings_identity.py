from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from harborrag_core.config_store import SettingsLevel, SettingsSnapshot, resolve_effective
from harborrag_core.ingestion.run_contracts import RunSettings
from harborrag_runtime.config import load_parser_catalog
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config_store.defaults import registry_for
from harborrag_runtime.config_store.run_settings import RunSettingsResolver
from harborrag_runtime.ingestion.profiles import build_processing_profile

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


def test_overlap_override_changes_chunk_strategy(shipped_settings: RuntimeSettings) -> None:
    pinned = build_processing_profile(
        shipped_settings, run_settings=RunSettings("rev_1", chunk_overlap_tokens=10)
    )

    assert pinned.chunk_strategy != GOLDEN_CHUNK_STRATEGY
    assert pinned.parser_profile == GOLDEN_PARSER_PROFILE


@pytest.mark.parametrize(
    ("overrides", "suffix"),
    [
        ({"ocr_enabled": False}, "+ocr-off"),
        ({"ocr_languages": ("en", "vi")}, "+ocr-en.vi"),
        ({"ocr_enabled": False, "ocr_languages": ("vi",)}, "+ocr-off-vi"),
        ({"ocr_enabled": True, "ocr_languages": ("en",)}, ""),
    ],
)
def test_ocr_override_suffixes_parser_profile_only_when_it_changes_a_parser(
    shipped_settings: RuntimeSettings, overrides: dict[str, object], suffix: str
) -> None:
    profile = build_processing_profile(
        shipped_settings, run_settings=RunSettings("rev_1", **overrides)
    )

    assert profile.parser_profile == f"{GOLDEN_PARSER_PROFILE}{suffix}"
    assert profile.chunk_strategy == GOLDEN_CHUNK_STRATEGY


def test_ocr_comparison_reads_a_yaml_language_list_and_a_missing_switch_as_on(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parsers.yaml"
    path.write_text(
        dedent(
            """
            version: 1
            parsers:
              pdf-docling:
                parser: pdf
                engines:
                  - backend: docling
                    settings:
                      ocr_lang: [en, vi]
            """
        ),
        encoding="utf-8",
    )

    catalog = load_parser_catalog(path)

    assert not catalog.ocr_overrides_change_parsing(ocr_enabled=True, ocr_languages=("en", "vi"))
    assert catalog.ocr_overrides_change_parsing(ocr_enabled=None, ocr_languages=("en",))
    assert catalog.ocr_overrides_change_parsing(ocr_enabled=False, ocr_languages=None)


def test_run_settings_json_round_trip() -> None:
    settings = RunSettings("rev_3", "docling", 256, 4)

    assert RunSettings.from_json(settings.to_json()) == settings
    assert RunSettings.from_json(RunSettings("rev_0").to_json()) == RunSettings("rev_0")
    assert RunSettings.from_json(None) is None


def test_run_settings_json_round_trip_with_capture_overrides() -> None:
    settings = RunSettings(
        "rev_3",
        max_document_mb=5,
        file_types=(".pdf", ".docx"),
        max_pdf_pages=10,
    )

    rebuilt = RunSettings.from_json(settings.to_json())

    assert rebuilt == settings
    assert rebuilt.file_types == (".pdf", ".docx")


def test_run_settings_json_round_trip_with_ocr_overrides() -> None:
    settings = RunSettings("rev_3", ocr_enabled=False, ocr_languages=("vi", "en"))

    rebuilt = RunSettings.from_json(settings.to_json())

    assert rebuilt == settings
    assert rebuilt.ocr_languages == ("vi", "en")


def test_run_settings_json_round_trip_with_overlap_and_embedding_batch_size() -> None:
    settings = RunSettings("rev_3", chunk_overlap_tokens=10, embedding_batch_size=8)

    rebuilt = RunSettings.from_json(settings.to_json())

    assert rebuilt == settings


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


@pytest.mark.asyncio
async def test_resolver_reports_capture_overrides() -> None:
    snapshot = SettingsSnapshot(
        "tenant-a",
        5,
        {
            "connection:p1/c1": {
                "capture.max_document_mb": 5,
                "capture.file_types": [".pdf", ".docx"],
                "capture.max_pdf_pages": 10,
            }
        },
    )

    run_settings = await RunSettingsResolver(_FixedProvider(snapshot)).for_connection(
        tenant_id="tenant-a", project_id="p1", connection_id="c1"
    )

    assert run_settings.max_document_mb == 5
    assert run_settings.file_types == (".pdf", ".docx")
    assert run_settings.max_pdf_pages == 10


@pytest.mark.asyncio
async def test_resolver_reports_ocr_overrides() -> None:
    snapshot = SettingsSnapshot(
        "tenant-a",
        5,
        {
            "connection:p1/c1": {
                "parsing.ocr.enabled": False,
                "parsing.ocr.languages": ["vi", "en"],
            }
        },
    )

    run_settings = await RunSettingsResolver(_FixedProvider(snapshot)).for_connection(
        tenant_id="tenant-a", project_id="p1", connection_id="c1"
    )

    assert run_settings.ocr_enabled is False
    assert run_settings.ocr_languages == ("vi", "en")


@pytest.mark.asyncio
async def test_resolver_reports_overlap_and_embedding_batch_size_overrides() -> None:
    snapshot = SettingsSnapshot(
        "tenant-a",
        5,
        {
            "connection:p1/c1": {"chunking.overlap_tokens": 10},
            # embedding.batch_size is tenant+project only (no connection level),
            # so its override must live at a level the key actually allows.
            "project:p1": {"embedding.batch_size": 8},
        },
    )

    run_settings = await RunSettingsResolver(_FixedProvider(snapshot)).for_connection(
        tenant_id="tenant-a", project_id="p1", connection_id="c1"
    )

    assert run_settings.chunk_overlap_tokens == 10
    assert run_settings.embedding_batch_size == 8


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
