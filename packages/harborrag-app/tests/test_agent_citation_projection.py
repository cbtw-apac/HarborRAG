"""Public agent citation projection and schema validation."""

from __future__ import annotations

from harborrag_app.api.v1.chat.schemas import ChatCompletionResponse
from harborrag_app.workflow_control.agent.support import result_data
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse
from harborrag_core.ports.agent_runs import AgentEvidenceReference, AgentStopReason
from harborrag_runtime.agent import AgentRunResult


def test_public_agent_result_reports_citation_validation() -> None:
    response = HarborChatResponse(
        id="answer",
        logical_model="primary",
        provider="mock",
        provider_model="mock-chat",
        deployment="private",
        message=HarborChatMessage.assistant("Claim with one valid and one invented source."),
        finish_reason="stop",
    )
    result = AgentRunResult(
        run_id="run-1",
        response=response,
        executions=(),
        turns=1,
        usage=response.usage,
        stop_reason=AgentStopReason.FINAL_ANSWER,
        citations=(
            AgentEvidenceReference(
                "vector_search",
                "chunk-1",
                "doc-1",
                0.9,
                "Deployment Guide",
                ("Operations", "Rollback"),
                "lines 40–46",
            ),
        ),
        citation_marker_count=2,
        invalid_citation_markers=('[Source: "Invented" — nowhere (ref 00000000)]',),
        citation_evidence_available=True,
    )

    data = result_data(result, session_id="session-1")
    public = ChatCompletionResponse.model_validate({**data, "mode": "agent"})

    assert public.citations[0].model_dump() == {
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "score": 0.9,
        "tool": "vector_search",
        "document_title": "Deployment Guide",
        "section_path": ("Operations", "Rollback"),
        "location": "lines 40–46",
        "marker": result.citations[0].marker,
    }
    assert public.citation_validation is not None
    assert public.citation_validation.model_dump() == {
        "complete": False,
        "evidence_available": True,
        "marker_count": 2,
        "validated_count": 1,
        "invalid_count": 1,
    }


def test_rag_schema_keeps_legacy_citation_shape_when_readable_fields_are_absent() -> None:
    public = ChatCompletionResponse.model_validate(
        {
            "id": "answer",
            "model": "primary",
            "provider": "mock",
            "provider_model": "mock-chat",
            "message": {"role": "assistant", "content": "Answer [Source 1]."},
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "citations": [{"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}],
            "session_id": "session-1",
        }
    )

    dumped = public.model_dump(mode="json")
    assert dumped["citations"] == [{"document_id": "doc-1", "chunk_id": "chunk-1", "score": 0.9}]
    assert "citation_validation" not in dumped


def test_citation_score_remains_a_required_openapi_field() -> None:
    schema = ChatCompletionResponse.model_json_schema()["$defs"]["ChatCitation"]

    assert "score" in schema["required"]
