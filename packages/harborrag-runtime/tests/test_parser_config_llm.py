from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from harborrag_runtime.config import (
    ParserConfigurationError,
    load_parser_catalog,
)

pytestmark = pytest.mark.unit


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "parsers.yaml"
    config_path.write_text(dedent(content), encoding="utf-8")
    return config_path


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


def test_build_registry_run_overrides_keep_the_chat_client_of_an_llm_chain(
    tmp_path: Path,
) -> None:
    from harborrag_adapters.parsers.pdf.engines.llm import LlmPDFEngine

    catalog = load_parser_catalog(_write_config(tmp_path, _LLM_CHAIN))

    registry = catalog.build_registry(max_pages=5, chat_client=_ScriptedChatClient())

    family = registry.resolve("report.pdf", None)
    assert isinstance(family.backends[0], LlmPDFEngine)
    assert family.page_limit.max_pages == 5


@pytest.mark.parametrize(
    ("settings", "named_in_error"),
    [
        ("{model: 5}", ("model", "text", "got 5")),
        ("{model: vision, max_pages: true}", ("max_pages", "an integer", "got True")),
        ("{model: vision, dpi: 600}", ("dpi", "between 72 and 300")),
    ],
)
def test_load_parser_catalog_invalid_llm_setting_value_is_rejected(
    tmp_path: Path,
    settings: str,
    named_in_error: tuple[str, ...],
) -> None:
    config_path = _write_config(
        tmp_path,
        f"""
        version: 1
        parsers:
          custom-pdf:
            parser: pdf
            engines:
              - backend: llm
                settings: {settings}
        """,
    )

    with pytest.raises(ParserConfigurationError) as error:
        load_parser_catalog(config_path)

    message = str(error.value)
    assert "parser 'custom-pdf' engine 0 (llm)" in message
    assert [name for name in named_in_error if name not in message] == []
