from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from harborrag_adapters.parsers.pdf_engine import (
    DoclingBackend,
    LiteParseBackend,
    MinerUBackend,
    PdfParser,
    PdfParserProfile,
    PyMuPdfBackend,
)
from harborrag_runtime.config import (
    ParserConfigurationError,
    load_parser_catalog,
)
from harborrag_runtime.config.parsers.models import (
    ParserDefinition,
    PdfBackendDefinition,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "parsers.yaml"
    config_path.write_text(dedent(content), encoding="utf-8")
    return config_path


def test_repository_config_builds_enabled_parser_overrides() -> None:
    catalog = load_parser_catalog(REPO_ROOT / "config" / "parsers.yaml")

    assert catalog.names(enabled_only=True) == ["image-rapidocr", "pdf-docling"]

    pdf_parser = catalog.build("pdf-docling")
    assert isinstance(pdf_parser, PdfParser)
    assert [backend.name for backend in pdf_parser.backends] == ["docling"]
    assert isinstance(pdf_parser.backends[0], DoclingBackend)
    assert pdf_parser.backends[0].options.ocr_engine == "rapidocr"

    image_parser = catalog.build("image-rapidocr")
    assert isinstance(image_parser, ImageParser)
    assert image_parser.ocr_engine == "rapidocr"

    harbor_parser = catalog.build_harbor_parser()
    assert isinstance(harbor_parser.create("pdf"), PdfParser)
    assert isinstance(harbor_parser.create("image"), ImageParser)
    assert harbor_parser.create("markdown") is not None


def test_repository_config_keeps_inactive_alternatives_commented() -> None:
    catalog = load_parser_catalog(REPO_ROOT / "config" / "parsers.yaml")

    assert catalog.names() == ["image-rapidocr", "pdf-docling"]


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


def test_backend_secrets_and_subprocess_environment_resolve_from_environment(
    tmp_path: Path,
) -> None:
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
              - backend: mineru
                settings:
                  backend: vlm-http-client
                  server_url: https://vlm.example.test/v1
                environment:
                  MINERU_VL_API_KEY: TEST_OPENAI_API_KEY
                  MINERU_VL_MODEL_NAME: TEST_OPENAI_VLM_MODEL
        """,
    )

    parser = load_parser_catalog(config_path).build(
        "protected-pdf",
        environment={
            "TEST_PDF_PASSWORD": "pdf-secret",
            "TEST_OPENAI_API_KEY": "api-secret",
            "TEST_OPENAI_VLM_MODEL": "document-vlm",
        },
    )

    assert isinstance(parser, PdfParser)
    assert isinstance(parser.backends[0], LiteParseBackend)
    assert parser.backends[0].options.password == "pdf-secret"
    assert isinstance(parser.backends[1], MinerUBackend)
    assert parser.backends[1].options.server_url == "https://vlm.example.test/v1"
    assert parser.backends[1].options.env == {
        "MINERU_VL_API_KEY": "api-secret",
        "MINERU_VL_MODEL_NAME": "document-vlm",
    }


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
            "supported only for the MinerU",
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
        backend="mineru",
        settings={"server_url": "https://secret.internal/v1"},
    )

    representation = repr(backend)

    assert "https://secret.internal/v1" not in representation
    assert "mineru" in representation


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

    with pytest.raises(ParserConfigurationError, match="both configure parser"):
        catalog.build_harbor_parser()
