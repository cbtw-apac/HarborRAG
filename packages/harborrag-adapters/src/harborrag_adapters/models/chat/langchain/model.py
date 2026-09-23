"""LangChain ``BaseChatModel`` facade over HarborRAG's asynchronous chat client.

The shim never touches a provider SDK: every call flows through
``AsyncHarborChatClient`` so routing, failover, budgets, and telemetry stay
owned by HarborRAG while LangChain and LangGraph orchestrate on top.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import BaseModel, ConfigDict, Field

from harborrag_adapters.models.chat.async_client import AsyncHarborChatClient
from harborrag_adapters.models.chat.validation import default_deployment
from harborrag_adapters.models.runtime.sync import run_awaitable_synchronously
from harborrag_core.models.chat import (
    HarborChatMetadata,
    HarborChatRequest,
    HarborChatStreamChunk,
    StreamEventType,
)

from .messages import (
    estimate_message_tokens,
    estimate_tokens,
    response_metadata,
    response_to_ai_message,
    to_harbor_messages,
    usage_metadata,
)
from .tools import (
    StructuredOutputMethod,
    ToolLike,
    compose_structured_output,
    json_schema_response_format,
    normalize_tool_choice,
    structured_output_parser,
    to_harbor_tools,
)

_REQUEST_FIELDS = frozenset(HarborChatRequest.model_fields) - {"messages", "logical_model"}
_SYNC_THREAD_NAME = "harborrag-langchain-chat"


class HarborChatModel(BaseChatModel):
    """Expose one HarborRAG logical chat model as a LangChain chat model.

    ``request_defaults`` carries ``HarborChatRequest`` fields (``temperature``,
    ``max_tokens``, ``tools``...) applied when a call does not override them;
    ``request_metadata`` (tenant, user, conversation identifiers) is attached to
    every request. ``sensitive`` defaults to ``True`` because conversation memory
    routinely contains personal data.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: AsyncHarborChatClient = Field(exclude=True)
    logical_model: str | None = None
    sensitive: bool = True
    request_defaults: dict[str, Any] = Field(default_factory=dict)
    request_metadata: HarborChatMetadata = Field(default_factory=HarborChatMetadata)

    def __init__(
        self,
        client: AsyncHarborChatClient,
        *,
        logical_model: str | None = None,
        sensitive: bool = True,
        request_defaults: Mapping[str, Any] | None = None,
        request_metadata: HarborChatMetadata | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        metadata = (
            request_metadata
            if isinstance(request_metadata, HarborChatMetadata)
            else HarborChatMetadata.model_validate(dict(request_metadata or {}))
        )
        payload: dict[str, Any] = {
            "client": client,
            "logical_model": logical_model,
            "sensitive": sensitive,
            "request_defaults": _validate_request_fields(request_defaults or {}),
            "request_metadata": metadata,
            **kwargs,
        }
        super().__init__(**payload)

    @property
    def _llm_type(self) -> str:
        return "harborrag-chat"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"logical_model": self.logical_model, "sensitive": self.sensitive}

    @property
    def supports_structured_output(self) -> bool:
        """Whether this model's resolved deployment declares ``structured_output``.

        Answered synchronously from the client's own configuration -- the same
        logical model and default deployment ``prepare_chat_request`` validates
        a call against -- so reading it costs no backend call and no I/O. That
        is the point: callers ask *before* they request structured output,
        instead of catching the ``HarborChatCapabilityError`` a deployment
        without the capability would raise, and per-turn control flow must not
        depend on an exception.

        Anything unresolvable answers ``False`` rather than raising: an unknown
        logical model, a logical model with no enabled deployment, a client
        whose configuration cannot be read. The ``except`` stays broad on
        purpose. Callers read this with ``getattr(model,
        "supports_structured_output", True)``, and ``getattr`` swallows
        ``AttributeError`` into that ``True`` default, so an ``AttributeError``
        escaping here would flip the answer to the opposite of the documented
        fallback. A wrong answer must only ever cost a type hint.
        """

        try:
            name, logical = self.client.config.model_for(self.logical_model)
            return default_deployment(name, logical).capabilities.structured_output
        except Exception:  # noqa: BLE001 - a capability hint must never break a turn
            return False

    def build_request(
        self,
        messages: Sequence[BaseMessage],
        *,
        stop: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> HarborChatRequest:
        """Build the validated HarborRAG request for one LangChain call."""

        values: dict[str, Any] = dict(self.request_defaults)
        values.update(_validate_request_fields(kwargs))
        if stop:
            values["stop"] = tuple(stop)
        values.setdefault("sensitive", self.sensitive)
        values.setdefault("metadata", self.request_metadata)
        return HarborChatRequest(
            messages=to_harbor_messages(messages),
            logical_model=self.logical_model,
            **values,
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        request = self.build_request(messages, stop=stop, **kwargs)
        response = await self.client.achat(request=request)
        message = response_to_ai_message(response)
        generation = ChatGeneration(
            message=message,
            generation_info={"finish_reason": message.response_metadata["finish_reason"]},
        )
        return ChatResult(generations=[generation], llm_output=response_metadata(response))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return run_awaitable_synchronously(
            self._agenerate(messages, stop=stop, run_manager=None, **kwargs),
            thread_name=_SYNC_THREAD_NAME,
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        request = self.build_request(messages, stop=stop, **kwargs)
        async for event in self.client.astream(request=request):
            chunk = _generation_chunk(event)
            if chunk is None:
                continue
            if run_manager is not None:
                await run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk

    def bind_tools(
        self,
        tools: Sequence[ToolLike],
        *,
        tool_choice: str | bool | dict[str, Any] | None = None,
        strict: bool | None = None,
        parallel_tool_calls: bool | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """Bind tool definitions, converting any LangChain tool spelling to HarborRAG tools."""

        harbor_tools = to_harbor_tools(tools, strict=strict)
        bound: dict[str, Any] = {"tools": harbor_tools, **kwargs}
        choice = normalize_tool_choice(tool_choice, harbor_tools)
        if choice is not None:
            bound["tool_choice"] = choice
        if parallel_tool_calls is not None:
            bound["parallel_tool_calls"] = parallel_tool_calls
        return self.bind(**bound)

    def with_structured_output(
        self,
        schema: dict[str, Any] | type,
        *,
        method: StructuredOutputMethod = "json_schema",
        include_raw: bool = False,
        strict: bool | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, dict[str, Any] | BaseModel]:
        """Return a runnable producing ``schema`` instances (or dicts for dict schemas).

        ``json_schema`` uses HarborRAG's native ``response_format`` path;
        ``function_calling`` forces a single tool call carrying the schema.
        """

        if kwargs:
            names = ", ".join(sorted(kwargs))
            raise ValueError(f"unsupported with_structured_output arguments: {names}")
        if method == "json_schema":
            response_format = json_schema_response_format(schema, strict=strict is not False)
            model: Runnable[Any, Any] = self.bind(response_format=response_format)
        elif method == "function_calling":
            model = self.bind_tools([schema], tool_choice=True, strict=strict)
        else:
            raise ValueError(f"unsupported structured output method: {method!r}")
        parser = structured_output_parser(schema, method)
        return compose_structured_output(model, parser, include_raw=include_raw)

    def get_num_tokens(self, text: str) -> int:
        """Estimate tokens in ``text`` without downloading a tokenizer."""

        return estimate_tokens(text)

    def get_num_tokens_from_messages(
        self,
        messages: list[BaseMessage],
        tools: Sequence[Any] | None = None,
    ) -> int:
        """Estimate prompt tokens so ``trim_messages`` can budget without a provider tokenizer."""

        total = estimate_message_tokens(messages)
        if tools:
            total += sum(
                estimate_tokens(tool.model_dump_json()) for tool in to_harbor_tools(list(tools))
            )
        return total


def _validate_request_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only ``HarborChatRequest`` fields, rejecting names the request cannot carry."""

    accepted: dict[str, Any] = {}
    unknown: list[str] = []
    for name, value in values.items():
        if name in _REQUEST_FIELDS:
            accepted[name] = value
        elif not name.startswith("ls_"):
            unknown.append(name)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unsupported HarborChatRequest parameters: {names}")
    return accepted


def _generation_chunk(event: HarborChatStreamChunk) -> ChatGenerationChunk | None:
    """Translate one normalized stream event into a LangChain generation chunk."""

    if event.event is StreamEventType.TEXT_DELTA and event.text_delta:
        return ChatGenerationChunk(message=AIMessageChunk(content=event.text_delta))
    if event.event is StreamEventType.TOOL_CALL_DELTA and event.tool_call_delta is not None:
        delta = event.tool_call_delta
        chunk = tool_call_chunk(
            name=delta.function.name or None,
            args=delta.function.arguments or None,
            id=delta.id or None,
            index=delta.index,
        )
        return ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[chunk]))
    if event.event is StreamEventType.COMPLETED:
        # The completed event repeats the final usage, so usage is attached once here
        # rather than on the intermediate USAGE event LangChain would sum twice.
        metadata: dict[str, Any] = {
            "logical_model": event.logical_model,
            "provider": event.provider,
            "provider_model": event.provider_model,
            "deployment": event.deployment,
            "finish_reason": event.finish_reason,
            "request_id": event.request_id,
            "response_id": event.response_id,
        }
        return ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                response_metadata=metadata,
                usage_metadata=usage_metadata(event.usage),
            ),
            generation_info={"finish_reason": event.finish_reason},
        )
    return None


__all__ = ["HarborChatModel"]
