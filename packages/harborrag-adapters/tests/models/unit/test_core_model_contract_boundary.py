import pytest

from harborrag_adapters.models.chat import AsyncHarborChatClient, HarborChatClient
from harborrag_adapters.models.embed import HarborEmbedClient
from harborrag_adapters.models.rerank import HarborRerankingClient
from harborrag_core.models.protocols import (
    AsyncHarborChatClientProtocol,
    AsyncHarborEmbedClientProtocol,
    AsyncHarborRerankClientProtocol,
    HarborChatClientProtocol,
    HarborEmbedClientProtocol,
    HarborRerankClientProtocol,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_adapter_clients_implement_core_protocols() -> None:
    assert issubclass(HarborChatClient, HarborChatClientProtocol)
    assert not issubclass(HarborChatClient, AsyncHarborChatClientProtocol)
    assert issubclass(AsyncHarborChatClient, AsyncHarborChatClientProtocol)
    assert not issubclass(AsyncHarborChatClient, HarborChatClientProtocol)
    assert issubclass(HarborEmbedClient, HarborEmbedClientProtocol)
    assert issubclass(HarborEmbedClient, AsyncHarborEmbedClientProtocol)
    assert issubclass(HarborRerankingClient, HarborRerankClientProtocol)
    assert issubclass(HarborRerankingClient, AsyncHarborRerankClientProtocol)
