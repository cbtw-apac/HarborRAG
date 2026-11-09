"""One chat turn's answer as the caller receives it, and the events that build it.

``ChatApplicationService.answer`` yields, in order: one ``RetrievalFound``, one or more
``AnswerDelta`` whose texts concatenate to the answer, and one ``TurnCompleted``. The
JSON route drains them; a stream sends each as it comes.

``RetrievalFound`` comes after every read and check the turn makes before its first
model call, and the next step is that call. A failure before it cost no model call.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from harborrag_core.domain.retrieval import RetrievalResult

from ..memory import ChatMode, CitationRecord


@dataclass(frozen=True, slots=True)
class CompletionUsage:
    """What the answer call cost; zero when no answer call was made."""

    mode: ChatMode
    steps: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float | None
    reasoning: str = "off"


@dataclass(frozen=True, slots=True)
class Completion:
    """One answered question; ``completion_id`` is the stored assistant message id."""

    completion_id: str
    session_id: str | None
    trace_id: str
    mode: ChatMode
    answer: str
    citations: tuple[CitationRecord, ...]
    usage: CompletionUsage

    def data(self) -> dict[str, object]:
        """The explorer completion body, fields in the explorer's order."""
        usage = self.usage
        return {
            "completion_id": self.completion_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "mode": self.mode,
            "answer": self.answer,
            "citations": [asdict(citation) for citation in self.citations],
            "usage": {
                "mode": usage.mode,
                "reasoning": usage.reasoning,
                "steps": usage.steps,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "cost_usd": usage.cost_usd,
            },
        }


@dataclass(frozen=True, slots=True)
class RetrievalFound:
    """The passages the answer may cite, found before any answer text exists."""

    results: tuple[RetrievalResult, ...]


@dataclass(frozen=True, slots=True)
class AnswerDelta:
    """The next piece of answer text, markers already numbered."""

    text: str


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """The answer is stored (in a session) and complete."""

    completion: Completion


type TurnEvent = RetrievalFound | AnswerDelta | TurnCompleted
