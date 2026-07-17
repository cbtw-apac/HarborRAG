from harborrag_adapters.models.chat import HarborChatClient
from harborrag_adapters.models.embed import HarborEmbedClient
from harborrag_adapters.models.rerank import HarborRerankingClient
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
    assert issubclass(HarborChatClient, AsyncHarborChatClientProtocol)
    assert issubclass(HarborEmbedClient, HarborEmbedClientProtocol)
    assert issubclass(HarborEmbedClient, AsyncHarborEmbedClientProtocol)
    assert issubclass(HarborRerankingClient, HarborRerankingClientProtocol)
    assert issubclass(HarborRerankingClient, AsyncHarborRerankingClientProtocol)
