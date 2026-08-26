from __future__ import annotations

from pydantic import Field

from harborrag_core.base import StrictModel

from .embed import EmbeddingPurpose


class HarborChatCapabilities(StrictModel):
    """Describe stable chat capabilities for one concrete deployment."""

    chat: bool = True
    streaming: bool = True
    tools: bool = False
    parallel_tools: bool = False
    structured_output: bool = False
    json_mode: bool = False
    multimodal: bool = False
    audio_input: bool = False
    reasoning_content: bool = False


class HarborEmbedCapabilities(StrictModel):
    """Describe stable embedding capabilities for one concrete deployment."""

    batch: bool = True
    token_inputs: bool = False
    configurable_dimensions: bool = False
    encoding_format: bool = False
    purpose: bool = False
    supported_purposes: frozenset[EmbeddingPurpose] = frozenset()
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_batch_size: int | None = Field(default=None, gt=0)
    default_dimensions: int | None = Field(default=None, gt=0)


class HarborRerankCapabilities(StrictModel):
    """Describe stable reranking capabilities for one concrete deployment."""

    structured_documents: bool = False
    rank_fields: bool = False
    return_documents: bool = True
    max_chunks_per_doc: bool = False
    max_tokens_per_doc: bool = False
    instruction: bool = False
    cost_tracking: bool = False
    max_documents: int | None = Field(default=None, gt=0)
