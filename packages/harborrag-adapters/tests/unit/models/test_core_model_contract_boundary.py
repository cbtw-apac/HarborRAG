from harborrag_adapters.models.chat import AsyncHarborChatClient, HarborChatClient
from harborrag_adapters.models.embed import AsyncHarborEmbedClient, HarborEmbedClient
from harborrag_adapters.models.rerank import (
    AsyncHarborRerankingClient,
    HarborRerankingClient,
)
from harborrag_core.models.protocols import (
    AsyncHarborChatClientProtocol,
    AsyncHarborEmbedClientProtocol,
    AsyncHarborRerankingClientProtocol,
    HarborChatClientProtocol,
    HarborEmbedClientProtocol,
    HarborRerankingClientProtocol,
)


def test_adapter_clients_implement_core_protocols() -> None:
    assert issubclass(HarborChatClient, HarborChatClientProtocol)
    assert issubclass(AsyncHarborChatClient, AsyncHarborChatClientProtocol)
    assert issubclass(HarborEmbedClient, HarborEmbedClientProtocol)
    assert issubclass(AsyncHarborEmbedClient, AsyncHarborEmbedClientProtocol)
    assert issubclass(HarborRerankingClient, HarborRerankingClientProtocol)
    assert issubclass(AsyncHarborRerankingClient, AsyncHarborRerankingClientProtocol)
