"""Persist and account for one chat turn, question and answer written apart.

The pair used to be written together, which meant an interrupted stream left
no trace at all: the caller had seen text and paid for it, and the next turn
had no idea the exchange happened. Splitting the writes gives each half its
own moment -- the question as soon as the turn is committed to, the answer as
soon as any text exists -- so nothing a caller was charged for is silently
discarded.

``turns_from_messages`` still derives pairs from these records, so a question
with no answer yet simply forms no turn and a marked-partial answer replays
like any other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import (
    HarborChatStreamChunk,
    HarborChatUsage,
    StreamEventType,
)
from harborrag_core.ports.conversation import ConversationMessage

from ..memory.identity import MemoryIdentity
from ..memory.messages import answer_message, append_message, question_message
from ..memory.usage import ModelCall, record_model_usage
from .preparation import ChatTurnResources
from .presenters import citation_data


@dataclass(frozen=True, slots=True)
class DeliveredAnswer:
    """The answer text a turn actually delivered, complete or not.

    ``call`` is what the provider reported about itself; it is ``None`` when a
    stream died before any usage chunk arrived, in which case there is nothing
    to account for. ``partial`` marks text that stopped early -- a deadline, a
    provider error, or a client that hung up.
    """

    text: str
    call: ModelCall | None = None
    partial: bool = False

    @property
    def completion_tokens(self) -> int | None:
        return None if self.call is None else self.call.usage.completion_tokens


@dataclass(slots=True)
class StreamedAnswer:
    """What a stream has delivered so far, readable at any point mid-flight.

    The service accumulates into this as chunks arrive so that every exit --
    completion, a provider error, the transport deadline, a client
    disconnect -- can describe the same partial state to one persistence path
    instead of each reconstructing it.
    """

    parts: list[str] = field(default_factory=list)
    last_chunk: HarborChatStreamChunk | None = None
    usage: HarborChatUsage | None = None
    finish_reason: str | None = None
    completed: bool = False

    def observe(self, chunk: HarborChatStreamChunk) -> None:
        """Fold one chunk in, keeping the newest model identity and usage."""

        self.last_chunk = chunk
        if chunk.text_delta:
            self.parts.append(chunk.text_delta)
        if chunk.usage is not None:
            self.usage = chunk.usage
        if chunk.finish_reason:
            self.finish_reason = str(chunk.finish_reason)
        if chunk.event is StreamEventType.COMPLETED:
            self.completed = True

    def delivered(self) -> DeliveredAnswer:
        """The answer as it stands, partial unless the stream said COMPLETED."""

        return DeliveredAnswer(
            "".join(self.parts),
            ModelCall.from_stream(
                self.last_chunk,
                usage=self.usage,
                finish_reason=self.finish_reason,
            ),
            partial=not self.completed,
        )


@dataclass(frozen=True, slots=True)
class RememberedTurn:
    """What survived of one turn's two writes.

    ``persisted`` is the user/assistant pair only when both writes landed:
    long-term extraction cites history message ids as provenance, so a fact
    must never outlive the messages it came from.
    """

    question: ConversationMessage | None
    answer: ConversationMessage | None
    # True when the turn produced no answer text at all, so there was nothing
    # to write -- distinct from a write that was attempted and failed.
    empty: bool = False

    @property
    def turn_persisted(self) -> bool:
        """What ``memory_persisted`` means to a caller: nothing was lost.

        Both halves count. A question that was not written leaves the answer
        standing alone in history, which the next turn replays as an assistant
        message with no prompt in front of it -- a lost turn, whichever half
        the store dropped.

        False only when a write was attempted and the store rejected it. A
        provider that returned no content has nothing to persist, and
        reporting that as a memory failure would tell the caller its healthy
        store was broken.
        """

        if self.question is None:
            return False
        return self.answer is not None or self.empty

    @property
    def persisted(self) -> tuple[ConversationMessage, ...] | None:
        if self.question is None or self.answer is None:
            return None
        return (self.question, self.answer)


async def remember_question(
    resources: ChatTurnResources,
    identity: MemoryIdentity,
    query: str,
) -> ConversationMessage | None:
    """Persist the question now that the turn is committed to.

    Called after the memory context has been built and before the model is
    called, so the window this turn replayed never contains this question.
    """

    return await append_message(
        resources.memory,
        identity.conversation(),
        question_message(query),
    )


async def remember_answer(
    resources: ChatTurnResources,
    identity: MemoryIdentity,
    answer: DeliveredAnswer,
    *,
    results: Sequence[RetrievalResult] = (),
) -> ConversationMessage | None:
    """Persist whatever answer text was delivered, marked partial when early.

    Empty text is not written: a stream that failed before its first delta
    produced no answer, and a blank assistant message would pair with the
    question as a completed turn and be replayed as one.
    """

    if not answer.text:
        return None
    return await append_message(
        resources.memory,
        identity.conversation(),
        answer_message(
            answer.text,
            citations=tuple(citation_data(result) for result in results),
            completion_tokens=answer.completion_tokens,
            partial=answer.partial,
        ),
    )


async def record_turn_usage(
    resources: ChatTurnResources,
    identity: MemoryIdentity,
    answer: DeliveredAnswer,
) -> bool:
    """Account for the tokens this turn spent, partial answers included.

    A partial answer's tokens were bought at the same price as a complete
    one's, so they are recorded whenever the provider reported them.
    """

    return await record_model_usage(resources.usage, identity, answer.call, surface="chat")


__all__ = [
    "DeliveredAnswer",
    "RememberedTurn",
    "StreamedAnswer",
    "record_turn_usage",
    "remember_answer",
    "remember_question",
]
