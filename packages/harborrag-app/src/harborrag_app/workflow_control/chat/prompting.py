"""Prompt assembly for retrieval-grounded chat completions."""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from harborrag_core.ports.conversation import ConversationMessage, ConversationTurn
from harborrag_core.ports.memory import Memory
from harborrag_runtime.memory import MemoryContext

from ..memory.identity import MemoryIdentity
from .memory_block import memory_block
from .presenters import citation_data

EVIDENCE_PREAMBLE = (
    "Supporting evidence retrieved for this question. It may be irrelevant: "
    "retrieval always returns its best matches, even when nothing in the "
    "indexed material is about what was asked. Ignore any source that does "
    "not bear on the question rather than letting it steer the answer."
)
"""Why the sources are offered rather than imposed.

Chat retrieval has no relevance gate -- the hybrid lane fuses by rank, so its
scores say nothing about whether a match is any good, and the raw similarity
that would is dropped before it reaches here. The prompt therefore has to be
what tells the model that a weak match is still a possible non-answer.
"""

NO_SOURCES = "No sources were retrieved for this question."

ROUTING = (
    "The sources may be different documents describing different things -- "
    "two similarly named pages can cover different teams, phases, or scopes. "
    "Attribute each claim to the source it came from and say which, rather "
    "than merging them into one answer. "
    "Answer from this conversation when the question is about the "
    "conversation or about the user. Use the sources for questions about the "
    "indexed material, and cite each one you use as [Source N]. When neither "
    "the conversation nor the sources answers the question, say so plainly "
    "instead of guessing."
)
"""Which source answers which question.

Without this the instruction was "answer from the retrieved context", so a
question the conversation had already answered -- the user's own name, said
one turn earlier -- was refused because no document happened to contain it.
"""


def build_chat_request(
    query: str,
    *,
    identity: MemoryIdentity,
    results: Sequence[RetrievalResult],
    context: MemoryContext | None = None,
    model: str | None = None,
) -> HarborChatRequest:
    """Fold remembered context, retrieved evidence, and the question into one request.

    ``context`` carries everything the memory policy decided for this turn:
    the verbatim window to replay, the rolling summary, recalled long-term
    memories, and the standalone query retrieval actually searched for.

    ``identity`` is passed whole rather than as loose identifiers because the
    metadata a telemetry sink reads needs the end user too: without
    ``user_id`` no request can be attributed to a human, only to the
    credential that happened to carry it.

    ``model`` is the logical model the caller chose, already validated against
    what its tenant may use. ``None`` leaves the field unset so the client
    resolves its configured default, which is what every caller got before
    model selection existed.
    """

    history = history_messages(context.messages) if context else ()
    summary = context.summary if context else None
    memories = context.recalled if context else ()
    retrieval_query = context.standalone_query if context else None
    request = HarborChatRequest(
        messages=(
            *history,
            HarborChatMessage.user(prompt_text(query, results, summary=summary, memories=memories)),
        ),
        logical_model=model,
        sensitive=True,
    )
    metadata = request.metadata.model_copy(
        update={
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "conversation_id": identity.session_id,
            "retrieval_query": retrieval_query or query,
            "document_ids": tuple(
                str(result.metadata.get("document_id", "")) for result in results
            ),
            "chunk_ids": tuple(result.id for result in results),
            "source_citations": tuple(citation_data(result) for result in results),
        }
    )
    return request.model_copy(update={"metadata": metadata})


def prompt_text(
    query: str,
    results: Sequence[RetrievalResult],
    *,
    summary: str | None = None,
    memories: Sequence[Memory] = (),
) -> str:
    """Fold retrieval into one explicitly delimited turn.

    The chat provider adapter renders every ``HarborChatMessage`` by role
    only (`build_litellm_messages`) -- it does not special-case
    ``context_kind``. Sending each chunk as its own
    ``HarborChatMessage.retrieved_context(...)`` message would therefore
    reach the model as an indistinguishable extra user turn. Folding
    everything into one labeled block keeps context and question
    unambiguous regardless of provider.
    """

    block = memory_block(summary, memories)
    prefix = f"{block}\n\n" if block else ""
    if not results:
        return f"{prefix}{NO_SOURCES}\n\n{ROUTING}\n\nQuestion: {query}"
    context = "\n\n".join(
        f"{_source_label(index, result)}\n{result.text}"
        for index, result in enumerate(results, start=1)
    )
    return (
        f"{prefix}{EVIDENCE_PREAMBLE}\n\nRetrieved context:\n{context}"
        f"\n\n{ROUTING}\n\nQuestion: {query}"
    )


def _source_label(index: int, result: RetrievalResult) -> str:
    """Identify one source by something a reader could tell apart.

    A bare ``document_id`` is an opaque hash, so two pages that differ only
    in scope -- an FE and a BE onboarding checklist -- reach the model as
    indistinguishable sources and get merged into one answer. The title and
    heading trail are already stored with the chunk; showing them is what
    lets the model attribute a claim to the right document.

    The id is kept as well: it is what the citation refers to, and a chunk
    ingested before titles were surfaced has nothing else to be known by.
    """

    metadata = result.metadata
    title = str(metadata.get("document_title") or "").strip()
    section = metadata.get("section_path")
    trail = (
        " > ".join(part for part in section if isinstance(part, str) and part.strip())
        if isinstance(section, list)
        else ""
    )
    parts = [f"[Source {index}]"]
    if title:
        parts.append(f'"{title}"')
    if trail:
        parts.append(f"({trail})")
    parts.append(f"(document_id={metadata.get('document_id', 'unknown')})")
    return " ".join(parts)


def turn_messages(turns: Sequence[ConversationTurn]) -> tuple[HarborChatMessage, ...]:
    """Replay completed turns as alternating user/assistant messages."""

    messages: list[HarborChatMessage] = []
    for turn in turns:
        messages.extend(
            (
                HarborChatMessage.user(turn.user_content),
                HarborChatMessage.assistant(turn.assistant_content),
            )
        )
    return tuple(messages)


def history_messages(messages: Sequence[ConversationMessage]) -> tuple[HarborChatMessage, ...]:
    """Replay stored per-message history as chat messages, oldest-first.

    Tool traffic is dropped: the chat surface exposes no tools, so a tool
    result would reach the model as an unexplained extra turn. The agent's
    tool history lives in its run checkpoint instead.
    """

    replayed: list[HarborChatMessage] = []
    for message in messages:
        if not message.content.strip():
            continue
        if message.role == "user":
            replayed.append(HarborChatMessage.user(message.content))
        elif message.role == "assistant":
            replayed.append(HarborChatMessage.assistant(message.content))
    return tuple(replayed)


__all__ = [
    "build_chat_request",
    "history_messages",
    "prompt_text",
    "turn_messages",
]
