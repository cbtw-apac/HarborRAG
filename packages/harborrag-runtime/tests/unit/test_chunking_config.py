"""chunking.yaml: strict loading, defaults, refiner choice and processing identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.chunking import RecursiveTextRefiner, RoutingTextRefiner, factory
from harborrag_runtime.config.chunking import (
    RefinerConfig,
    chunking_settings_from,
    default_chunking_settings,
    load_chunking_settings,
)
from harborrag_runtime.config.errors import ChunkingConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion import chunking_profile
from harborrag_runtime.ingestion.chunking_profile import (
    build_refiner,
    chunk_strategy_fingerprint,
    refiner_fingerprint,
)
from harborrag_runtime.ingestion.profiles import build_processing_profile
from harborrag_runtime.tokenization import ApproximateTokenCounter

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SHIPPED = REPOSITORY_ROOT / "config" / "chunking.yaml"
SCAFFOLD = (
    REPOSITORY_ROOT
    / "packages/harborrag-app/src/harborrag_app/scaffold/templates/chunking.yaml.tmpl"
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "chunking.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_file_and_scaffold_declare_exactly_the_built_in_defaults() -> None:
    defaults = default_chunking_settings()

    for path in (SHIPPED, SCAFFOLD):
        loaded = load_chunking_settings(path)
        assert loaded.config == defaults.config
        assert loaded.refiner == defaults.refiner
    assert SHIPPED.read_text(encoding="utf-8") == SCAFFOLD.read_text(encoding="utf-8")


def test_omitted_sections_keep_the_defaults(tmp_path: Path) -> None:
    loaded = load_chunking_settings(_write(tmp_path, "version: 1\n"))

    assert loaded.config == default_chunking_settings().config
    assert loaded.refiner == RefinerConfig()
    assert loaded.source_path == tmp_path / "chunking.yaml"


def test_profiles_and_refiner_are_read_from_the_file(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """version: 1
default_profile: small
create_route_chunks: false
refiner:
  strategy: recursive
profiles:
  small:
    strategy: canonical
    limits: {minimum_tokens: 10, target_tokens: 200, maximum_tokens: 300, overlap_tokens: 20}
    repeat_table_headers: false
source_profiles:
  Jira: small
""",
    )

    loaded = load_chunking_settings(path)

    profile = loaded.config.profiles["small"]
    assert (profile.target_tokens, profile.maximum_tokens, profile.overlap_tokens) == (200, 300, 20)
    assert profile.soft_maximum_tokens == 300
    assert profile.repeat_table_headers is False
    assert dict(loaded.config.source_profiles) == {"jira": "small"}
    assert loaded.config.create_route_chunks is False
    assert loaded.refiner.strategy == "recursive"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("version: 2\n", "version"),
        ("version: 1\nunknown: 1\n", "unknown"),
        ("version: 1\nrefiner: {strategy: semantic}\n", "strategy"),
        ("version: 1\nversion: 1\n", "duplicate"),
        (
            "version: 1\nprofiles:\n  p: {strategy: canonical, limits: "
            "{minimum_tokens: 1, target_tokens: 50, maximum_tokens: 40}}\n",
            "maximum_tokens must not be below target_tokens",
        ),
        ("version: 1\ndefault_profile: missing\n", "default_profile"),
        ("version: 1\nsource_profiles: {jira: missing}\n", "unknown profiles"),
        (
            "version: 1\nprofiles:\n  p: {strategy: canonical, limits: {target_tokens: '5'}}\n",
            "limits",
        ),
    ],
)
def test_invalid_files_are_rejected_with_the_path(tmp_path: Path, text: str, message: str) -> None:
    path = _write(tmp_path, text)

    with pytest.raises(ChunkingConfigurationError, match=message) as error:
        load_chunking_settings(path)
    assert "chunking" in str(error.value).lower()


def test_absent_default_path_uses_defaults_but_an_explicit_missing_path_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert chunking_settings_from(RuntimeSettings()) == default_chunking_settings()
    with pytest.raises(ChunkingConfigurationError, match="does not exist"):
        chunking_settings_from(RuntimeSettings(chunking_config_path=tmp_path / "missing.yaml"))


def test_refiner_choice_builds_the_matching_refiner(monkeypatch) -> None:
    monkeypatch.setattr(chunking_profile, "grammar_set_problems", lambda: [])
    monkeypatch.setattr(factory, "grammar_set_problems", lambda: [])
    counter = ApproximateTokenCounter()

    assert isinstance(
        build_refiner(counter, RefinerConfig(strategy="recursive")), RecursiveTextRefiner
    )
    assert isinstance(build_refiner(counter, RefinerConfig()), RoutingTextRefiner)
    assert refiner_fingerprint(RefinerConfig(strategy="recursive")) == "recursive:2"
    assert refiner_fingerprint().startswith("routing:1(")


def test_missing_code_grammars_stop_ingestion_unless_the_fallback_is_chosen(
    monkeypatch,
) -> None:
    missing = ["tree-sitter-go missing"]
    monkeypatch.setattr(chunking_profile, "grammar_set_problems", lambda: missing)
    monkeypatch.setattr(factory, "grammar_set_problems", lambda: missing)
    counter = ApproximateTokenCounter()

    assert RefinerConfig().require_code_grammars is True
    with pytest.raises(ChunkingConfigurationError, match="tree-sitter-go missing") as error:
        build_refiner(counter, RefinerConfig())
    assert "require_code_grammars: false" in str(error.value)
    assert isinstance(
        build_refiner(counter, RefinerConfig(require_code_grammars=False)), RoutingTextRefiner
    )


def test_refiner_choice_is_part_of_processing_identity(tmp_path: Path) -> None:
    parser = tmp_path / "parsers.yaml"
    parser.write_text("version: 1\n", encoding="utf-8")
    structure = _write(tmp_path, "version: 1\n")
    recursive = tmp_path / "recursive.yaml"
    recursive.write_text("version: 1\nrefiner: {strategy: recursive}\n", encoding="utf-8")
    grammars = tmp_path / "grammars.yaml"
    grammars.write_text("version: 1\nrefiner: {require_code_grammars: false}\n", encoding="utf-8")

    def identity(path: Path) -> str:
        settings = RuntimeSettings(parser_config_path=parser, chunking_config_path=path)
        return build_processing_profile(settings).chunk_strategy

    assert identity(structure) != identity(recursive)
    # Requiring grammars changes what fails at startup, not how text splits.
    assert identity(structure) == identity(grammars)
    assert identity(structure) == chunk_strategy_fingerprint(default_chunking_settings().config)
