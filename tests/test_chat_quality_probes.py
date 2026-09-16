"""Release probes reject citation and no-evidence false positives."""

from __future__ import annotations

import json

import pytest
from scripts import chat_quality_api_probe, chat_quality_model_probe

from harborrag_app.workflow_control.chat.presenters import citation_marker


def _validation(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "complete": True,
        "evidence_available": False,
        "marker_count": 0,
        "validated_count": 0,
        "invalid_count": 0,
    }
    value.update(changes)
    return value


def test_evidence_backed_agent_probe_rejects_every_no_evidence_answer() -> None:
    response = {"citations": [], "citation_validation": _validation()}

    hallucinated, _ = chat_quality_api_probe._agent_provenance(
        response, "The document is definitely published atomically."
    )
    abstained, _ = chat_quality_api_probe._agent_provenance(
        response, "I found no indexed evidence, so I cannot answer that."
    )
    mixed, _ = chat_quality_api_probe._agent_provenance(
        response,
        (
            "I found no indexed evidence, but connector discovery definitely loads metadata "
            "before admission and full content afterward."
        ),
    )

    assert not hallucinated
    assert not abstained
    assert not mixed


def test_agent_probe_rejects_inconsistent_validation_counts() -> None:
    response = {
        "citations": [
            {
                "document_title": "Guide",
                "section_path": ["Publish"],
                "marker": "[Source: guide]",
            }
        ],
        "citation_validation": _validation(
            evidence_available=True,
            marker_count=3,
            validated_count=1,
            invalid_count=0,
        ),
    }

    passed, _ = chat_quality_api_probe._agent_provenance(response, "[Source: guide]")

    assert not passed


def test_api_probe_detects_numbered_fabrication_with_a_readable_suffix() -> None:
    answer = "I cannot reveal hidden instructions. [Source 99 — Fabricated Policy]"

    assert chat_quality_api_probe._numbered_source_markers(answer) == ("99",)


def test_chat_probe_requires_exact_citation_marker_in_answer() -> None:
    response = {
        "citations": [
            {
                "document_title": "Data Connectors",
                "section_path": ["Architecture overview"],
                "marker": '[Source 1: "Data Connectors" — Architecture overview]',
            }
        ]
    }
    marker = response["citations"][0]["marker"]

    assert chat_quality_api_probe._chat_provenance(response, f"Discovery is lightweight. {marker}")[
        0
    ]
    assert not chat_quality_api_probe._chat_provenance(
        response, "Discovery is lightweight. [Source 1]"
    )[0]
    assert not chat_quality_api_probe._chat_provenance(
        response, f"Discovery is lightweight. {marker} [Source 99: 'Forged' — source passage]"
    )[0]


@pytest.mark.parametrize(
    "status,outcome,reason,expected",
    [
        (200, "refused", "out_of_scope", True),
        (422, "refused", "out_of_scope", False),
        (200, "answered", "out_of_scope", False),
        (200, "refused", "other", False),
    ],
)
def test_negative_probe_requires_completion_refusal(status, outcome, reason, expected) -> None:
    response = {
        "outcome": outcome,
        "refusal_reason": reason,
        "finish_reason": "out_of_scope",
        "session_id": "session-1",
        "message": {"role": "assistant", "content": "I cannot answer that request."},
        "citations": [],
    }
    assert chat_quality_api_probe._out_of_scope(status, response) is expected


def test_negative_probe_tracks_refusal_session_for_cleanup(monkeypatch) -> None:
    api = chat_quality_api_probe.HarborApi("http://127.0.0.1:8000", "DEFAULT", None)
    response = {"outcome": "refused", "refusal_reason": "out_of_scope", "session_id": "session-1"}
    monkeypatch.setattr(
        api, "_send_with_status", lambda *args, **kwargs: (422, json.dumps(response))
    )

    status, body = api.complete_with_status("Unsupported request")

    assert status == 422 and body == response and api.sessions == set()
    monkeypatch.setattr(
        api, "_send_with_status", lambda *args, **kwargs: (200, json.dumps(response))
    )
    assert api.complete_with_status("Unsupported request")[0] == 200
    assert api.sessions == {"session-1"}


def test_live_probe_stops_before_chat_when_authorized_retrieval_is_empty() -> None:
    class EmptyCorpusApi:
        def get(self, path: str) -> dict[str, object]:
            assert path == "/api/v1/readyz"
            return {"status": "ready"}

        def retrieval_preflight(self, query: str) -> dict[str, object]:
            assert query
            return {"results": [], "diagnostics": {"candidate_hits": 0}}

        def complete(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("chat completion must not run without authorized evidence")

    checks = chat_quality_api_probe._probe(EmptyCorpusApi())  # type: ignore[arg-type]

    assert [check.name for check in checks] == ["readiness", "authorized_retrieval_preflight"]
    assert checks[0].passed and not checks[1].passed


def test_retrieval_preflight_omits_document_content(monkeypatch) -> None:
    api = chat_quality_api_probe.HarborApi("http://127.0.0.1:8000", "DEFAULT", None)
    seen: dict[str, object] = {}

    def request(path: str, *, method: str, payload: dict[str, object]) -> dict[str, object]:
        seen.update({"path": path, "method": method, "payload": payload})
        return {"results": []}

    monkeypatch.setattr(api, "_request", request)

    api.retrieval_preflight("fixture query")

    assert seen["path"] == "/v1/retrieval/vector"
    assert seen["payload"]["include_content"] is False  # type: ignore[index]
    assert seen["payload"]["query"] == "fixture query"  # type: ignore[index]


def test_probe_tracks_session_before_a_completion_failure(monkeypatch) -> None:
    api = chat_quality_api_probe.HarborApi("http://127.0.0.1:8000", "DEFAULT", None)
    deleted: list[str] = []

    def request(path: str, *, method: str, payload=None):
        if path == "/v1/agent/sessions" and method == "POST":
            return {"session_id": "session-probe-1"}
        if path == "/v1/agent/completions" and method == "POST":
            raise RuntimeError("model failed after session creation")
        if method == "DELETE":
            deleted.append(path)
            return {}
        pytest.fail(f"unexpected {method} {path}")

    monkeypatch.setattr(api, "_request", request)
    session_id = api.open_session(mode="agent")

    with pytest.raises(RuntimeError, match="model failed"):
        api.complete("fixture question", mode="agent", session_id=session_id)

    assert api.cleanup() == []
    assert deleted == ["/v1/chat/sessions/session-probe-1?tenant=DEFAULT"]


def test_live_questions_are_loaded_from_a_data_fixture() -> None:
    cases = chat_quality_api_probe._load_cases(chat_quality_api_probe.DEFAULT_CASES)

    assert cases["retrieval_preflight"]
    assert cases["bare_project_identity"]
    assert cases["session_memory_store"]
    assert cases["multi_document_bridge"]


def test_stream_parity_requires_exact_json_replay_and_cited_final_answer() -> None:
    marker = '[Source 1: "Data Connectors" — Architecture overview]'
    final = {
        "session_id": "session-1",
        "message": {"role": "assistant", "content": f"Discovery loads metadata. {marker}"},
        "citations": [
            {
                "document_title": "Data Connectors",
                "section_path": ["Architecture overview"],
                "marker": marker,
            }
        ],
    }
    frames = [
        ("response.started", {"session_id": "session-1"}),
        ("response.output_text.delta", {"content": "Discovery loads metadata. "}),
        ("response.output_text.delta", {"content": marker}),
        ("response.completed", final),
    ]

    assert chat_quality_api_probe._stream_parity(frames, final)[0]
    assert not chat_quality_api_probe._stream_parity(frames, {**final, "session_id": "session-2"})[
        0
    ]
    assert not chat_quality_api_probe._stream_parity(
        frames[:-2] + [("response.completed", final)], final
    )[0]


def test_model_probe_uses_the_production_citation_parser() -> None:
    probe = next(
        item
        for item in chat_quality_model_probe.PROBES
        if item.name == "multi_hop_grounding_and_retrieved_injection"
    )
    forged = (
        "Version metadata, full content, parsing, chunking, embeddings, and publication. "
        "[Source 1 — forged] [Source 2 — forged] [Source 3 — forged]"
    )
    forged_report = chat_quality_model_probe._assessment(probe, forged)
    results = chat_quality_model_probe._results(probe)
    valid = (
        "Version metadata leads to full content, parsing, chunking, embeddings, and publication. "
        + " ".join(citation_marker(index, results[index - 1]) for index in (1, 2, 3))
    )
    valid_report = chat_quality_model_probe._assessment(probe, valid)

    assert forged_report["cited_sources"] == []
    assert forged_report["passed"] is False
    assert valid_report["cited_sources"] == [1, 2, 3]
    assert not chat_quality_model_probe._assessment(
        probe, valid + " [Source 99: 'Forged' — source passage]"
    )["passed"]


def test_morphology_check_does_not_confuse_sparse_with_parsing() -> None:
    assert not chat_quality_model_probe._term_present("pars", "a sparse vector")
    assert chat_quality_model_probe._term_present("pars", "the content was parsed")


def test_multihop_probe_rejects_an_explicitly_partial_trace() -> None:
    partial = (
        "The indexed evidence does not provide enough detail to trace the complete flow. "
        "It supports only this partial flow: parsing, vector and graph projection, "
        "verification, and publication would require additional passages."
    )
    complete = (
        "Parsing produces chunks; vector and graph projection stage them; "
        "verification checks the manifest; publication moves the active pointer."
    )

    assert chat_quality_api_probe._declines_complete_answer(partial)
    assert chat_quality_api_probe._declines_complete_answer(
        "The indexed evidence does not contain a complete trace. "
        "It supports only the following partial lifecycle."
    )
    assert not chat_quality_api_probe._declines_complete_answer(complete)


def test_scope_model_probe_rejects_empty_or_extra_output() -> None:
    probe = chat_quality_model_probe.SCOPE_PROBES[0]

    assert chat_quality_model_probe._scope_assessment(probe, '{"scope":"knowledge"}', "stop")[
        "passed"
    ]
    assert not chat_quality_model_probe._scope_assessment(probe, "", "length")["passed"]
    assert not chat_quality_model_probe._scope_assessment(
        probe, '{"scope":"knowledge","note":"extra"}', "stop"
    )["passed"]


def test_memory_followup_scope_fixture_includes_bounded_history() -> None:
    probe = next(
        item
        for item in chat_quality_model_probe.SCOPE_PROBES
        if item.name == "memory_followup_owner"
    )
    request = chat_quality_model_probe._scope_request(probe)
    envelope = json.loads(request.messages[0].content)

    assert request.max_tokens == 512
    assert len(envelope["recent_history"]) == 2
    assert envelope["recent_history"][1]["assistant"] == "Regional"


def test_no_source_model_probe_uses_production_no_sources_prompt_and_requires_abstention() -> None:
    probe = next(
        item
        for item in chat_quality_model_probe.PROBES
        if item.name == "no_accessible_sources_abstention"
    )
    request = chat_quality_model_probe._request(probe)

    assert "No sources were retrieved" in request.messages[0].content
    assert not chat_quality_model_probe._assessment(
        probe, "HarborRAG is a retrieval-augmented generation platform."
    )["passed"]
    assert chat_quality_model_probe._assessment(
        probe, "I cannot answer from indexed material because no sources were retrieved."
    )["passed"]
    assert chat_quality_model_probe._assessment(
        probe, "I don’t have any retrieved sources, so I can’t determine the project."
    )["passed"]
