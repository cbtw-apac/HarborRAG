from __future__ import annotations

import pytest
from model_runtime_support import FakeChatInvocation, chat_config

from harborrag_core.models.chat import HarborChatMessage

from .chat_client_support import sync_client
from .test_client_execution import raw_chat

pytestmark = pytest.mark.unit


def test_chat_call_turns_off_sdk_retries() -> None:
    invocation = FakeChatInvocation([raw_chat()])

    sync_client(chat_config(), backend=invocation).chat([HarborChatMessage.user("hello")])

    assert invocation.calls[0]["max_retries"] == 0
