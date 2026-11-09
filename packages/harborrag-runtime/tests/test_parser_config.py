from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent

import pytest

from harborrag_adapters.parsers.compat import (
    DoclingBackend,
    LiteParseBackend,
    PdfParser,
    PdfParserProfile,
    PyMuPdfBackend,
)
from harborrag_adapters.parsers.image.parser import HarborImageParser
from harborrag_runtime.config import (
    ParserConfigurationError,
    load_parser_catalog,
)
from harborrag_runtime.config.parsers.schemas import (
    ParserDefinition,
    PdfBackendDefinition,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "parsers.yaml"
    config_path.write_text(dedent(content), encoding="utf-8")
    return config_path


def test_repository_config_builds_enabled_parser_overrides(caplog, monkeypatch) -> None:
    monkeypatch.delenv("HARBORRAG_OCR_SERVER_URL", raising=False)
    with caplog.at_level(logging.INFO, logger="harborrag.runtime.config.parsers"):
        catalog = load_parser_catalog(REPO_ROOT / "config" / "ingestion" / "parsers.yaml")

    assert catalog.names(enabled_only=True) == ["image-liteparse", "pdf-liteparse"]

    pdf_parser = catalog.build("pdf-liteparse")
    assert isinstance(pdf_parser, PdfParser)
    assert [backend.name for backend in pdf_parser.backends] == ["liteparse"]
    assert isinstance(pdf_parser.backends[0], LiteParseBackend)
    # Without HARBORRAG_OCR_SERVER_URL no OCR server is used: LiteParse OCRs locally.
    assert pdf_parser.backends[0].options.ocr_server_url is None

    image_parser = catalog.build("image-liteparse")
    assert isinstance(image_parser, HarborImageParser)
    assert image_parser.ocr_engine == "liteparse"
    assert image_parser.engines[0].ocr_server_url is None

    harbor_parser = catalog.build_harbor_parser()
    attachment_parser = catalog.build_harbor_parser()
    assert isinstance(harbor_parser.create("pdf"), PdfParser)
    assert attachment_parser is not harbor_parser
    assert isinstance(harbor_parser.create("image"), HarborImageParser)
    assert harbor_parser.resolve("README.md", "text/markdown") is not None
    assert "Parser catalog loaded" in caplog.text
    assert "definitions=2 enabled=2" in caplog.text


def test_repository_config_keeps_inactive_alternatives_commented() -> None:
    catalog = load_parser_catalog(REPO_ROOT / "config" / "ingestion" / "parsers.yaml")

    assert catalog.names() == ["image-liteparse", "pdf-liteparse"]


def test_ocr_server_url_follows_the_environment_over_the_catalog_default(monkeypatch) -> None:
    monkeypatch.setenv("HARBORRAG_OCR_SERVER_URL", "http://localhost:8888/ocr")

    catalog = load_parser_catalog(REPO_ROOT / "config" / "ingestion" / "parsers.yaml")

    assert catalog.build("pdf-liteparse").backends[0].options.ocr_server_url == (
        "http://localhost:8888/ocr"
    )
    assert catalog.build("image-liteparse").engines[0].ocr_server_url == (
        "http://localhost:8888/ocr"
    )


def test_an_empty_ocr_server_url_means_no_ocr_server(monkeypatch) -> None:
    monkeypatch.setenv("HARBORRAG_OCR_SERVER_URL", "  ")

    catalog = load_parser_catalog(REPO_ROOT / "config" / "ingestion" / "parsers.yaml")

    liteparse = catalog.build("pdf-liteparse").backends[0]
    assert liteparse.options.ocr_server_url is None
    assert "ocr_server_url" not in liteparse._constructor_kwargs()
    assert catalog.build("image-liteparse").engines[0].ocr_server_url is None


def test_setting_reference_to_an_unset_variable_without_a_default_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("TEST_OCR_SERVER_URL", raising=False)
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        parsers:
          custom-image:
            parser: image
            settings:
              ocr_engine: liteparse
              ocr_server_url: ${TEST_OCR_SERVER_URL}
        """,
    )

    with pytest.raises(ParserConfigurationError, match="TEST_OCR_SERVER_URL"):
        load_parser_catalog(config_path)


def test_builds_explicit_pdf_backend_order_and_options(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        parsers:
          custom-pdf:
            parser: pdf
            settings:
              min_content_chars: 40
            engines:
              - backend: pymupdf
              - backend: docling
                settings:
                  do_ocr: true
                  force_full_page_ocr: true
        """,
    )

    parser = load_parser_catalog(config_path).build("custom-pdf")

    assert isinstance(parser, PdfParser)
    assert parser.min_content_chars == 40
    assert [backend.name for backend in parser.backends] == ["pymupdf", "docling"]
    assert isinstance(parser.backends[0], PyMuPdfBackend)
    assert isinstance(parser.backends[1], DoclingBackend)
    assert parser.backends[1].options.force_full_page_ocr is True


def test_build_allows_explicit_code_overrides(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        parsers:
          fast-pdf:
            parser: pdf
            settings:
              profile: fast
              min_content_chars: 20
        """,
    )
    catalog = load_parser_catalog(config_path)

    parser = catalog.build("fast-pdf", overrides={"min_content_chars": 100})

    assert isinstance(parser, PdfParser)
    assert parser.profile == PdfParserProfile.FAST
    assert parser.min_content_chars == 100


def test_backend_secrets_resolve_from_environment(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        parsers:
          protected-pdf:
            parser: pdf
            engines:
              - backend: liteparse
                secrets:
                  password_env: TEST_PDF_PASSWORD
        """,
    )

    parser = load_parser_catalog(config_path).build(
        "protected-pdf",
        environment={"TEST_PDF_PASSWORD": "pdf-secret"},
    )

    assert isinstance(parser, PdfParser)
    assert isinstance(parser.backends[0], LiteParseBackend)
    assert parser.backends[0].options.password == "pdf-secret"


def test_missing_backend_environment_reference_fails_at_build(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        parsers:
          protected-pdf:
            parser: pdf
            engines:
              - backend: liteparse
                secrets:
                  password_env: TEST_PDF_PASSWORD
        """,
    )
    catalog = load_parser_catalog(config_path)

    with pytest.raises(ParserConfigurationError, match="TEST_PDF_PASSWORD"):
        catalog.build("protected-pdf", environment={})


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("parsers: {}", "version must be 1"),
        ("version: 2\nparsers: {}", "Unsupported parser configuration version"),
        (
            """
            version: 1
            parsers:
              broken:
                parser: unknown
            """,
            "unsupported parser",
        ),
        (
            """
            version: 1
            parsers:
              broken:
                parser: markdown
                settings:
                  profile: fast
            """,
            "unknown markdown setting",
        ),
        (
            """
            version: 1
            parsers:
              broken:
                parser: markdown
                engines:
                  - backend: pymupdf
            """,
            "engines only when parser is 'pdf'",
        ),
        (
            """
            version: 1
            parsers:
              broken:
                parser: pdf
                engines:
                  - backend: unknown
            """,
            "unsupported PDF backend",
        ),
        (
            """
            version: 1
            parsers:
              broken:
                parser: pdf
                engines:
                  - backend: pymupdf
                    settings:
                      unknown: true
            """,
            "unknown pymupdf setting",
        ),
        (
            """
            version: 1
            parsers:
              broken:
                parser: pdf
                settings:
                  profile: quality
                engines:
                  - backend: pymupdf
            """,
            "cannot combine",
        ),
        (
            """
            version: 1
            parsers:
              broken:
                parser: pdf
                engines:
                  - backend: liteparse
                    settings:
                      password: secret
            """,
            "cannot store secret",
        ),
        (
            """
            version: 1
            parsers:
              broken:
                parser: pdf
                engines:
                  - backend: docling
                    environment:
                      OPENAI_API_KEY: OPENAI_API_KEY
            """,
            "environment",
        ),
    ],
)
def test_rejects_invalid_parser_configuration(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    with pytest.raises(ParserConfigurationError, match=message):
        load_parser_catalog(_write_config(tmp_path, content))


def test_parser_definition_repr_hides_settings() -> None:
    definition = ParserDefinition(
        name="secretive",
        parser="pdf",
        settings={"api_key": "super-secret-value"},
    )

    representation = repr(definition)

    assert "super-secret-value" not in representation
    assert "secretive" in representation


def test_pdf_backend_definition_repr_hides_settings() -> None:
    backend = PdfBackendDefinition(
        backend="liteparse",
        settings={"ocr_server_url": "https://secret.internal/ocr"},
    )

    representation = repr(backend)

    assert "https://secret.internal/ocr" not in representation
    assert "liteparse" in representation


def test_registry_rejects_multiple_enabled_profiles_for_same_parser(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        parsers:
          fast:
            parser: pdf
            settings:
              profile: fast
          quality:
            parser: pdf
            settings:
              profile: quality
        """,
    )
    catalog = load_parser_catalog(config_path)

    with pytest.raises(ParserConfigurationError, match="both configure family"):
        catalog.build_harbor_parser()


@pytest.mark.parametrize(
    ("parser", "kept_suffix", "kept_engine"),
    [
        ("docx", ".doc", "doc"),
        ("docx", ".odt", "odt"),
        ("docx", ".epub", "epub"),
        ("epub", ".docx", "docx"),
        ("csv", ".xlsx", "excel"),
        ("html", ".md", "markdown"),
        ("pptx", ".ppt", "ppt"),
    ],
)
def test_configuring_one_engine_keeps_the_rest_of_its_family(
    tmp_path: Path, parser: str, kept_suffix: str, kept_engine: str
) -> None:
    config_path = _write_config(
        tmp_path,
        f"""
        version: 1
        parsers:
          configured:
            parser: {parser}
        """,
    )

    registry = load_parser_catalog(config_path).build_harbor_parser()

    family = registry.resolve(f"file{kept_suffix}", None)
    [engine] = [engine for engine in family.engines if kept_suffix in engine.suffixes]
    assert engine.name == kept_engine


@pytest.mark.parametrize("profile", ["liteparse", "fast", "balanced", "quality"])
def test_a_named_pdf_profile_sends_scanned_pages_to_the_ocr_server(
    tmp_path: Path, profile: str
) -> None:
    config_path = _write_config(
        tmp_path,
        f"""
        version: 1
        parsers:
          pdf-profile:
            parser: pdf
            settings:
              profile: {profile}
              ocr_server_url: http://ppocr-server:8888/ocr
        """,
    )

    parser = load_parser_catalog(config_path).build("pdf-profile")

    [liteparse] = [backend for backend in parser.backends if backend.name == "liteparse"]
    assert liteparse.options.ocr_server_url == "http://ppocr-server:8888/ocr"


def test_an_explicit_engines_chain_sets_the_ocr_server_on_its_liteparse_engine(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        parsers:
          pdf-chain:
            parser: pdf
            settings:
              ocr_server_url: http://ppocr-server:8888/ocr
            engines:
              - backend: liteparse
        """,
    )

    with pytest.raises(ParserConfigurationError, match="liteparse engine"):
        load_parser_catalog(config_path)


_LLM_CHAIN = """
    version: 1
    parsers:
      pdf-llm:
        parser: pdf
        engines:
          - backend: llm
            settings:
              model: vision
              max_pages: 10
          - backend: pymupdf
    """


class _ScriptedChatClient:
    """Stands in for the runtime's sync chat client; raises ``error`` on every call."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def chat(self, messages=None, **kwargs):
        raise self.error or AssertionError("no reply scripted")

    def close(self) -> None:
        pass


def _one_page_pdf() -> bytes:
    import fitz

    document = fitz.open()
    try:
        document.new_page().insert_text((72, 72), "Quarterly revenue grew in every region")
        return document.tobytes()
    finally:
        document.close()


def test_an_llm_engine_names_its_chat_model_and_uses_the_runtime_client(tmp_path: Path) -> None:
    from harborrag_adapters.parsers.pdf.engines.llm import LlmPDFEngine

    catalog = load_parser_catalog(_write_config(tmp_path, _LLM_CHAIN))

    parser = catalog.build("pdf-llm", chat_client=_ScriptedChatClient())

    assert catalog.chat_models() == {"vision"}
    assert [backend.name for backend in parser.backends] == ["llm", "pymupdf"]
    assert isinstance(parser.backends[0], LlmPDFEngine)
    assert parser.backends[0].options.max_pages == 10


def test_an_llm_engine_built_without_a_chat_client_is_a_configuration_error(
    tmp_path: Path,
) -> None:
    catalog = load_parser_catalog(_write_config(tmp_path, _LLM_CHAIN))

    with pytest.raises(ParserConfigurationError, match="'llm' is invalid: it reads pages"):
        catalog.build_harbor_parser()


def test_a_disabled_llm_definition_needs_no_chat_model(tmp_path: Path) -> None:
    disabled = _LLM_CHAIN.replace("parser: pdf", "parser: pdf\n        enabled: false")

    catalog = load_parser_catalog(_write_config(tmp_path, disabled))

    assert catalog.chat_models() == frozenset()
    assert catalog.build_harbor_parser() is not None


def test_a_rate_limited_page_fails_the_parse_stage_as_retryable(tmp_path: Path) -> None:
    from harborrag_core.contracts.errors import HarborUnavailableError
    from harborrag_core.domain.parser import ParseInput
    from harborrag_core.models.errors import HarborChatRateLimitError
    from harborrag_runtime.ingestion.failure_classification import ingestion_failure_classifier

    catalog = load_parser_catalog(_write_config(tmp_path, _LLM_CHAIN))
    registry = catalog.build_harbor_parser(
        chat_client=_ScriptedChatClient(HarborChatRateLimitError("slow down"))
    )

    with pytest.raises(HarborUnavailableError) as raised:
        registry.parse(ParseInput(content=_one_page_pdf(), filename="report.pdf"))

    failure = ingestion_failure_classifier().classify("ParseAndNormalize", raised.value)
    assert failure.retryable is True


_MODELS_YAML = """
chat:
  default_model: primary
  security:
    allowed_providers: [openai]
  models:
    primary:
      deployments:
        - name: text-only
          provider: openai
          model: gpt-text
          api_key: test-key
    vision:
      deployments:
        - name: vision
          provider: openai
          model: gpt-vision
          api_key: test-key
          capabilities:
            multimodal: true
"""


@pytest.mark.parametrize("model", ["primary", "missing"])
def test_the_parser_chat_client_refuses_a_model_that_cannot_read_images(
    tmp_path: Path, model: str
) -> None:
    from harborrag_runtime.chat.composition import build_parser_chat_client
    from harborrag_runtime.config.settings import RuntimeSettings

    models_path = tmp_path / "models.yaml"
    models_path.write_text(_MODELS_YAML, encoding="utf-8")
    settings = RuntimeSettings(model_config_path=models_path)

    with pytest.raises(ParserConfigurationError, match=f"not usable: {model}"):
        build_parser_chat_client(settings, {model})

    client = build_parser_chat_client(settings, {"vision"})
    client.close()
