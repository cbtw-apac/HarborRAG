from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import fitz
import pytest

from harborrag_adapters.parsers.errors import (
    MaxPagesExceededError,
    NoExtractableTextError,
)
from harborrag_adapters.parsers.pdf.engines.llm import LlmPDFConfig, LlmPDFEngine
from harborrag_adapters.parsers.pdf.engines.pymupdf import PyMuPDFEngine
from harborrag_adapters.parsers.pdf.parser import HarborPDFParser
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.domain.parser import ParseInput
from harborrag_core.models.chat import (
    FinishReason,
    HarborChatMessage,
    HarborChatResponse,
    ImageURLContentPart,
    TextContentPart,
)
from harborrag_core.models.errors import (
    HarborChatAuthenticationError,
    HarborChatRateLimitError,
)

pytestmark = pytest.mark.unit

PAGE_TEXT = "Quarterly revenue grew in every region this year"


def _pdf(pages: int) -> bytes:
    document = fitz.open()
    try:
        for number in range(1, pages + 1):
            document.new_page().insert_text((72, 72), f"{PAGE_TEXT} page {number}")
        return document.tobytes()
    finally:
        document.close()


class _FakeChatClient:
    """Answers each call with the next scripted reply, or raises it."""

    def __init__(
        self, replies: Sequence[str | Exception], finish: FinishReason = FinishReason.STOP
    ):
        self.replies = list(replies)
        self.finish = finish
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: Sequence[HarborChatMessage] | None = None,
        *,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborChatResponse:
        self.calls.append({"messages": messages, "model": model, **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return HarborChatResponse(
            id="r",
            logical_model="vision",
            provider="fake",
            provider_model="fake-vision",
            deployment="d",
            message=HarborChatMessage.assistant(reply),
            finish_reason=self.finish,
        )


def _engine(client: _FakeChatClient, **options: Any) -> LlmPDFEngine:
    return LlmPDFEngine(LlmPDFConfig(model="vision", **options), chat_client=client)  # type: ignore[arg-type]


def _input(pages: int) -> ParseInput:
    return ParseInput(content=_pdf(pages), filename="report.pdf")


def test_each_page_is_sent_as_an_image_and_comes_back_as_its_markdown() -> None:
    client = _FakeChatClient(
        ["# Revenue\n\nGrew 12%.", "| Region | Growth |\n|---|---|\n| EU | 9% |"]
    )

    result = _engine(client).parse_input(_input(2))

    assert result.content == (
        "Page 1\n# Revenue\n\nGrew 12%.\n\nPage 2\n| Region | Growth |\n|---|---|\n| EU | 9% |"
    )
    assert [element.metadata["page"] for element in result.elements] == [1, 2]
    assert result.metadata == {"page_count": 2}
    first = client.calls[0]
    assert (first["model"], first["temperature"], first["sensitive"]) == ("vision", 0, True)
    prompt, image = first["messages"][0].content
    assert isinstance(prompt, TextContentPart)
    assert isinstance(image, ImageURLContentPart)
    assert image.image_url.url.startswith("data:image/png;base64,")


def test_a_code_fence_around_the_whole_reply_is_dropped() -> None:
    client = _FakeChatClient(["```markdown\n# Revenue\n```"])

    assert _engine(client).parse_input(_input(1)).content == "Page 1\n# Revenue"


def test_a_pdf_over_max_pages_is_left_to_the_next_engine_without_any_model_call() -> None:
    client = _FakeChatClient([])

    with pytest.raises(MaxPagesExceededError):
        _engine(client, max_pages=1).parse_input(_input(2))

    assert client.calls == []


def test_a_reply_cut_at_the_output_limit_is_kept_with_a_warning() -> None:
    client = _FakeChatClient(["# Revenue"], finish=FinishReason.LENGTH)

    result = _engine(client).parse_input(_input(1))

    assert result.content == "Page 1\n# Revenue"
    assert result.warnings == ["page 1: transcription stopped at the model's output limit"]


def test_blank_replies_for_every_page_are_no_extractable_text() -> None:
    with pytest.raises(NoExtractableTextError):
        _engine(_FakeChatClient(["", "  "])).parse_input(_input(2))


def test_options_reject_a_blank_model_and_out_of_range_limits() -> None:
    for options in ({"model": " "}, {"model": "v", "max_pages": 0}, {"model": "v", "dpi": 600}):
        with pytest.raises(ValueError):
            LlmPDFConfig(**options)


def test_a_retryable_model_failure_escapes_the_chain_instead_of_falling_back() -> None:
    client = _FakeChatClient([HarborChatRateLimitError("slow down")])
    parser = HarborPDFParser(backends=[_engine(client), PyMuPDFEngine()])

    with pytest.raises(HarborUnavailableError, match="page 1") as raised:
        parser.parse_input(_input(1))

    assert isinstance(raised.value.__cause__, HarborChatRateLimitError)


def test_a_permanent_model_failure_falls_back_without_the_provider_text(caplog) -> None:
    provider_text = "invalid key sk-provider-body"
    client = _FakeChatClient([HarborChatAuthenticationError(provider_text)])
    parser = HarborPDFParser(backends=[_engine(client), PyMuPDFEngine()])

    with caplog.at_level(logging.DEBUG):
        document = parser.parse_input(_input(1))

    assert PAGE_TEXT in document.content
    assert document.metadata["pdf_engine"] == "pymupdf"
    assert "llm: failed (model 'vision' failed on page 1 (HarborChatAuthenticationError))" in (
        document.warnings
    )
    recorded = caplog.text + str(document.warnings) + str(document.metadata)
    assert "provider-body" not in recorded


def test_the_chain_accepts_a_transcribed_table_over_the_text_fallback() -> None:
    table = (
        "## Revenue by region\n\n| Region | Q1 | Q2 |\n|---|---|---|\n"
        "| EU | 1.2 | 1.4 |\n| APAC | 0.8 | 1.1 |"
    )
    parser = HarborPDFParser(backends=[_engine(_FakeChatClient([table])), PyMuPDFEngine()])

    document = parser.parse_input(_input(1))

    assert document.metadata["pdf_engine"] == "llm"
    assert "| APAC | 0.8 | 1.1 |" in document.content
