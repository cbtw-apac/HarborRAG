"""Release probes reject citation and no-evidence false positives."""

from __future__ import annotations

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


def test_model_probe_uses_the_production_citation_parser() -> None:
    probe = chat_quality_model_probe.PROBES[0]
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


def test_morphology_check_does_not_confuse_sparse_with_parsing() -> None:
    assert not chat_quality_model_probe._term_present("pars", "a sparse vector")
    assert chat_quality_model_probe._term_present("pars", "the content was parsed")
