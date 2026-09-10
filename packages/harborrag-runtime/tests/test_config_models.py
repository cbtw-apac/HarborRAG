from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_runtime.config import describe_model_catalog

CATALOG = """
chat:
  default_model: primary
  backend: {type: direct_sdk}
  security: {allowed_providers: [openai]}
  models:
    primary:
      deployments:
        - name: openai-chat
          provider: openai
          model: openai/gpt-4o-mini
          api_key: ${TEST_OPENAI_KEY}
          capabilities: {streaming: true, structured_output: true, json_mode: true, tools: true}
embed:
  default_model: primary
  security: {allowed_providers: [openai]}
  models:
    primary:
      embedding_space: test-v1
      deployments:
        - name: openai-embedding
          provider: openai
          model: openai/text-embedding-3-small
          api_key: ${TEST_OPENAI_KEY}
          expected_dimensions: 1536
          capabilities: {batch: true, encoding_format: true}
"""


def test_describes_chat_and_embed_sections(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    path = tmp_path / "models.yaml"
    path.write_text(CATALOG)

    summary = describe_model_catalog(path)

    assert summary.chat_default == "primary"
    assert summary.chat_deployments == ("openai-chat",)
    assert summary.embed_deployments == ("openai-embedding",)


def test_missing_environment_reference_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TEST_OPENAI_KEY", raising=False)
    path = tmp_path / "models.yaml"
    path.write_text(CATALOG)

    with pytest.raises(Exception, match="TEST_OPENAI_KEY"):
        describe_model_catalog(path)
