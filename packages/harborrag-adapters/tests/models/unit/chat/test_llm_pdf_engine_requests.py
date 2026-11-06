from __future__ import annotations

import fitz
import pytest

from harborrag_adapters.parsers.pdf.engines.llm import LlmPDFConfig, LlmPDFEngine
from harborrag_core.domain.parser import ParseInput
from harborrag_core.models.capabilities import HarborChatCapabilities

from .chat_client_support import FakeInvocation, response_dict, sync_client

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def _one_page_pdf() -> bytes:
    document = fitz.open()
    try:
        document.new_page().insert_text((72, 72), "Quarterly revenue grew in every region")
        return document.tobytes()
    finally:
        document.close()


def test_the_real_chat_client_sends_a_page_image_at_temperature_zero(base_config) -> None:
    logical = base_config.models["primary"]
    vision = logical.deployments[0].model_copy(
        update={"capabilities": HarborChatCapabilities(multimodal=True)}
    )
    config = base_config.model_copy(
        update={"models": {"primary": logical.model_copy(update={"deployments": (vision,)})}}
    )
    invocation = FakeInvocation([response_dict("# Revenue\n\n| Region | Growth |")])
    client = sync_client(config, backend=invocation)

    engine = LlmPDFEngine(LlmPDFConfig(model="primary"), chat_client=client)
    result = engine.parse_input(ParseInput(content=_one_page_pdf(), filename="report.pdf"))

    assert result.content == "Page 1\n# Revenue\n\n| Region | Growth |"
    [call] = invocation.calls
    assert call["temperature"] == 0
    assert "sensitive" not in call
    prompt, image = call["messages"][0]["content"]
    assert prompt["type"] == "text"
    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    client.close()
