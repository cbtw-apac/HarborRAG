"""LangChain bindings for HarborRAG conversation memory.

The memory package stays free of LLM-provider SDKs: the model reaches it as an
injected ``langchain_core`` ``BaseChatModel`` and persistence goes through the
core ``ConversationMessageStore`` port.
"""

from .converters import (
    from_langchain_message,
    from_langchain_messages,
    to_langchain_message,
    to_langchain_messages,
)
from .history import HarborChatMessageHistory

__all__ = [
    "HarborChatMessageHistory",
    "from_langchain_message",
    "from_langchain_messages",
    "to_langchain_message",
    "to_langchain_messages",
]
