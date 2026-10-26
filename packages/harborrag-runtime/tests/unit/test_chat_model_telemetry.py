"""Runtime chat owns the telemetry it creates, including failed construction."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_runtime.chat import composition
from harborrag_runtime.config.settings import RuntimeSettings


@pytest.mark.parametrize("fail", [False, True])
def test_chat_composition_owns_and_cleans_telemetry(monkeypatch, fail) -> None:
    config = HarborChatClientConfig.from_dict(
        {"default_model": "primary", "models": {"primary": {"provider": "openai", "model": "m"}}}
    )
    telemetry = Mock()
    telemetry_builder = Mock(return_value=telemetry)
    factory = Mock(side_effect=ValueError("invalid configuration") if fail else None)
    monkeypatch.setattr(composition.HarborChatClientConfig, "from_file", Mock(return_value=config))
    monkeypatch.setattr(composition, "build_model_telemetry", telemetry_builder)
    monkeypatch.setattr(composition.ChatClientFactory, "create_async", factory)

    settings = RuntimeSettings(langfuse_enabled=True)
    if fail:
        with pytest.raises(ValueError, match="invalid configuration"):
            composition.build_chat_client(settings)
        telemetry.close.assert_called_once()
    else:
        assert composition.build_chat_client(settings) is factory.return_value
        telemetry.close.assert_not_called()

    telemetry_builder.assert_called_once_with(config, langfuse_enabled=True)
    dependencies = factory.call_args.args[1]
    assert dependencies.telemetry is telemetry
    assert dependencies.telemetry_ownership is ResourceOwnership.OWNED
