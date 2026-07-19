from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import Any, NoReturn

import pytest
from harborrag_core.models.capabilities import (
    HarborChatCapabilities,
    HarborEmbedCapabilities,
    HarborRerankCapabilities,
)
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatUsage,
    ImageURL,
    ImageURLContentPart,
    StructuredOutputDegradation,
    TextContentPart,
)
from harborrag_core.models.context import ModelOperationContext
from harborrag_core.models.embed import (
    EmbeddingEncodingFormat,
    HarborEmbedRequest,
    HarborEmbedUsage,
)
from harborrag_core.models.errors import (
    HarborChatRateLimitError,
    HarborEmbedPartialBatchError,
    HarborModelError,
)
from harborrag_core.models.protocols import (
    AsyncHarborChatClientProtocol,
    AsyncHarborEmbedClientProtocol,
    AsyncHarborRerankingClientProtocol,
    HarborChatClientProtocol,
    HarborEmbedClientProtocol,
    HarborRerankingClientProtocol,
)
from harborrag_core.models.rerank import (
    HarborRerankDocument,
    HarborRerankRequest,
    HarborRerankUsage,
)
from harborrag_core.models.usage import ModelTokenUsage
from pydantic import SecretStr, ValidationError


class ContractClient:
    def chat(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    async def achat(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    def stream(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    def astream(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    def chat_structured(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    async def achat_structured(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    def embed(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    async def aembed(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    def rerank(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    async def arerank(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise NotImplementedError

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def test_client_protocols_are_runtime_structural_contracts() -> None:
    client = ContractClient()
    assert isinstance(client, HarborChatClientProtocol)
    assert isinstance(client, AsyncHarborChatClientProtocol)
    assert isinstance(client, HarborEmbedClientProtocol)
    assert isinstance(client, AsyncHarborEmbedClientProtocol)
    assert isinstance(client, HarborRerankingClientProtocol)
    assert isinstance(client, AsyncHarborRerankingClientProtocol)


def test_request_contracts_validate_and_protect_credentials() -> None:
    chat = HarborChatRequest(
        messages=[HarborChatMessage.user("hello")],
        custom_headers={"Authorization": "Bearer secret"},
    )
    assert isinstance(chat.custom_headers["Authorization"], SecretStr)

    multimodal = HarborChatMessage.user(
        (
            TextContentPart(text="describe this"),
            ImageURLContentPart(image_url=ImageURL(url="https://example.com/image.png")),
        )
    )
    assert isinstance(multimodal.content, tuple)
    assert StructuredOutputDegradation.JSON_MODE.value == "json_mode"

    embed = HarborEmbedRequest(inputs=("hello", (1, 2, 3)))
    assert embed.inputs == ("hello", (1, 2, 3))
    with pytest.raises(ValidationError):
        HarborEmbedRequest(
            inputs=("hello",),
            normalize=True,
            encoding_format=EmbeddingEncodingFormat.BASE64,
        )

    document = HarborRerankDocument.text("candidate", document_id="doc-1")
    rerank = HarborRerankRequest(query="question", documents=(document,), top_n=1)
    assert rerank.documents[0].document_id == "doc-1"
    with pytest.raises(ValidationError):
        HarborRerankRequest(query="question", documents=(document,), top_n=2)


def test_usage_context_capability_and_error_contracts_are_shared() -> None:
    for usage in (HarborChatUsage(), HarborEmbedUsage(), HarborRerankUsage()):
        assert isinstance(usage, ModelTokenUsage)
        assert usage.total_tokens == 0

    context = ModelOperationContext(request_id="request-1", logical_model="primary")
    context.provider = "provider"
    context.state["attempt"] = 1
    assert context.provider == "provider"
    assert context.state == {"attempt": 1}

    assert HarborChatCapabilities().chat is True
    assert HarborEmbedCapabilities().batch is True
    assert HarborRerankCapabilities().return_documents is True

    error = HarborChatRateLimitError("limited", request_id="request-1")
    assert isinstance(error, HarborModelError)
    assert error.retryable is True
    assert error.to_dict()["request_id"] == "request-1"
    assert issubclass(HarborEmbedPartialBatchError, HarborModelError)


def test_core_has_no_model_provider_dependencies_or_imports() -> None:
    package_root = Path(__file__).parents[1]
    document = tomllib.loads((package_root / "pyproject.toml").read_text("utf-8"))
    dependencies = document["project"]["dependencies"]
    banned = {
        "anthropic",
        "boto3",
        "google",
        "langfuse",
        "litellm",
        "openai",
        "opentelemetry",
    }
    assert not any(
        dependency.split("[")[0].split("<")[0].split(">")[0].split("=")[0] in banned
        for dependency in dependencies
    )

    imported: set[str] = set()
    for source in (package_root / "src" / "harborrag_core").rglob("*.py"):
        tree = ast.parse(source.read_text("utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(banned)
