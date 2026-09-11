"""Every scaffolded file must load through the real runtime loaders."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from harborrag_app.scaffold import (
    PRESETS,
    InitOptions,
    ScaffoldExistsError,
    build_scaffold,
    write_scaffold,
)
from harborrag_runtime.config import load_connector_catalog, load_parser_catalog
from harborrag_runtime.config.temporal_loading import load_temporal_config

EXPECTED_FILES = {
    "harborrag.yaml",
    ".env",
    ".gitignore",
    "docker-compose.yml",
    "config/connectors.yaml",
    "config/models.yaml",
    "config/parsers.yaml",
    "config/temporal.yaml",
}


def _options(provider: str) -> InitOptions:
    preset = PRESETS[provider]
    return InitOptions(
        provider=provider,
        chat_model=preset.default_chat_model,
        embed_model=preset.default_embed_model,
        api_key="sk-test",
        api_base="https://gateway.example.com/v1" if preset.requires_api_base else None,
        source_path="./docs",
    )


def test_scaffold_produces_the_documented_layout() -> None:
    files = build_scaffold(_options("openai"), created_at="2026-09-10")
    assert set(files) == EXPECTED_FILES


@pytest.mark.parametrize("provider", sorted(PRESETS))
def test_rendered_catalogs_load_through_the_runtime(
    provider: str, tmp_path: Path, monkeypatch
) -> None:
    from harborrag_adapters.models.chat.configs import HarborChatClientConfig
    from harborrag_adapters.models.embed.configs import HarborEmbedClientConfig

    files = build_scaffold(_options(provider), created_at="2026-09-10")
    write_scaffold(tmp_path, files, force=False)
    for line in (tmp_path / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)

    catalog = load_connector_catalog(tmp_path / "config/connectors.yaml")
    assert tuple(catalog.names(enabled_only=True)) == ("workspace",)
    load_parser_catalog(tmp_path / "config/parsers.yaml")
    load_temporal_config(tmp_path / "config/temporal.yaml")
    chat = HarborChatClientConfig.from_file(tmp_path / "config/models.yaml")
    embed = HarborEmbedClientConfig.from_file(tmp_path / "config/models.yaml")
    assert chat.default_model == "primary"
    assert embed.default_model == "primary"
    compose = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {"qdrant", "falkordb", "minio"}


def test_dotenv_carries_generated_secrets_and_the_provider_key() -> None:
    files = build_scaffold(_options("openai"), created_at="2026-09-10")
    env = files[".env"]
    assert "OPENAI_API_KEY=sk-test" in env
    key_line = next(
        line for line in env.splitlines() if line.startswith("HARBORRAG_SECRETS_ENCRYPTION_KEY=")
    )
    assert len(key_line.split("=", 1)[1]) == 64
    assert "LOCAL_SOURCE_PATH=./docs" in env


def test_write_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    files = build_scaffold(_options("openai"), created_at="2026-09-10")
    write_scaffold(tmp_path, files, force=False)
    with pytest.raises(ScaffoldExistsError):
        write_scaffold(tmp_path, files, force=False)


def test_force_never_overwrites_an_existing_dotenv(tmp_path: Path) -> None:
    files = build_scaffold(_options("openai"), created_at="2026-09-10")
    write_scaffold(tmp_path, files, force=False)
    (tmp_path / ".env").write_text("KEEP=me\n")

    written = write_scaffold(tmp_path, files, force=True)

    assert (tmp_path / ".env").read_text() == "KEEP=me\n"
    assert (tmp_path / ".env.new").exists()
    assert tmp_path / ".env.new" in written


def test_ports_offset_moves_every_published_port_and_disables_grpc() -> None:
    options = InitOptions(
        provider="openai",
        chat_model="gpt-4o-mini",
        embed_model="text-embedding-3-small",
        api_key="sk-test",
        api_base=None,
        source_path="./docs",
        ports_offset=10000,
    )
    files = build_scaffold(options, created_at="2026-09-10")
    compose = yaml.safe_load(files["docker-compose.yml"])
    published = sorted(
        port for service in compose["services"].values() for port in service["ports"]
    )
    assert published == [
        "127.0.0.1:16333:6333",
        "127.0.0.1:16334:6334",
        "127.0.0.1:16379:6379",
        "127.0.0.1:19000:9000",
        "127.0.0.1:19001:9001",
    ]
    assert "HARBORRAG_QDRANT_PREFER_GRPC=false" in files[".env"]


def _options_for(sources: tuple[str, ...], **values: str) -> InitOptions:
    return InitOptions(
        provider="openai",
        chat_model="gpt-4o-mini",
        embed_model="text-embedding-3-small",
        api_key="sk-test",
        api_base=None,
        source_path="./docs",
        sources=sources,
        source_values=values,
    )


@pytest.mark.parametrize(
    ("sources", "expected_names"),
    [
        (("local",), ["workspace"]),
        (("github",), ["github"]),
        (("confluence", "jira"), ["confluence", "jira"]),
        (("local", "github", "confluence", "jira"), ["confluence", "github", "jira", "workspace"]),
    ],
)
def test_selected_sources_render_loadable_connector_blocks(
    sources: tuple[str, ...], expected_names: list[str], tmp_path: Path, monkeypatch
) -> None:
    files = build_scaffold(_options_for(sources), created_at="2026-09-10")
    write_scaffold(tmp_path, files, force=False)
    monkeypatch.chdir(tmp_path)

    catalog = load_connector_catalog(tmp_path / "config/connectors.yaml")

    assert catalog.names(enabled_only=True) == expected_names
    env = files[".env"]
    assert ("LOCAL_SOURCE_PATH=" in env) == ("local" in sources)
    assert ("GITHUB_TOKEN=" in env) == ("github" in sources)
    assert ("CONFLUENCE_SPACE_KEY=" in env) == ("confluence" in sources)
    assert ("JIRA_BASE_URL=" in env) == ("jira" in sources)


def test_source_values_land_in_dotenv_and_jira_project_keys_in_yaml() -> None:
    files = build_scaffold(
        _options_for(
            ("github", "jira"),
            GITHUB_REPOSITORY_URL="https://github.com/acme/docs",
            GITHUB_TOKEN="ghp_x",
            JIRA_PROJECT_KEYS="ENG, OPS",
        ),
        created_at="2026-09-10",
    )
    assert "GITHUB_REPOSITORY_URL=https://github.com/acme/docs" in files[".env"]
    assert "GITHUB_TOKEN=ghp_x" in files[".env"]
    assert "project_keys: [ENG, OPS]" in files["config/connectors.yaml"]
    assert "JIRA_PROJECT_KEYS" not in files[".env"]


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="sharepoint"):
        build_scaffold(_options_for(("sharepoint",)), created_at="2026-09-10")
