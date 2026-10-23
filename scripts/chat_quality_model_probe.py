"""Live model probe for HarborRAG's grounded chat prompt.

Run this only with a configured model environment. It supplies controlled
passages directly to the same prompt builder and chat runtime used by the API,
so grounding can be checked even when a deployment corpus is unavailable.
No repository or indexed data is read or written.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from harborrag_app.workflow_control.chat.evidence import ChatEvidence
from harborrag_app.workflow_control.chat.presenters import citation_marker, cited_results
from harborrag_app.workflow_control.chat.prompting import prompt_text
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import HarborChatMessage, HarborChatMetadata, HarborChatRequest
from harborrag_runtime.chat import ChatPrompt, RuntimeChatService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.contracts import RetrievalResponse
from harborrag_runtime.sdk import RetrievalLane


@dataclass(frozen=True, slots=True)
class Probe:
    name: str
    question: str
    passages: tuple[str, ...]
    required_terms: tuple[str, ...]
    required_sources: tuple[int, ...]
    forbidden_terms: tuple[str, ...] = ()
    allowed_sources: tuple[int, ...] | None = None
    abstention_required: bool = False


@dataclass(frozen=True, slots=True)
class ScopeProbe:
    name: str
    question: str
    expected: str
    recent_history: tuple[dict[str, str], ...] = ()


CASE_FILE = Path(__file__).with_name("chat_quality_model_cases.json")


def _load_cases(path: Path) -> tuple[tuple[Probe, ...], tuple[ScopeProbe, ...]]:
    """Load controlled questions and expected evidence from a data fixture."""

    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("grounding"), list)
        or not isinstance(data.get("scope"), list)
    ):
        raise ValueError("model quality cases require grounding and scope arrays")
    grounding = tuple(
        Probe(
            name=item["name"],
            question=item["question"],
            passages=tuple(item["passages"]),
            required_terms=tuple(item["required_terms"]),
            required_sources=tuple(item["required_sources"]),
            forbidden_terms=tuple(item.get("forbidden_terms", ())),
            allowed_sources=(tuple(item["allowed_sources"]) if "allowed_sources" in item else None),
            abstention_required=item.get("abstention_required", False),
        )
        for item in data["grounding"]
    )
    scopes = tuple(
        ScopeProbe(
            name=item["name"],
            question=item["question"],
            expected=item["expected"],
            recent_history=tuple(item.get("recent_history", ())),
        )
        for item in data["scope"]
    )
    if (
        not grounding
        or not scopes
        or any(not item.name or not item.question for item in (*grounding, *scopes))
    ):
        raise ValueError("model quality cases must contain named, nonempty questions")
    return grounding, scopes


PROBES, SCOPE_PROBES = _load_cases(CASE_FILE)

_TERM_PATTERNS = {
    "pars": re.compile(r"\b(?:parse(?:d|s)?|parsing|parser(?:s)?)\b"),
    "embed": re.compile(r"\b(?:embed(?:s|ded|ding)?|embedding(?:s)?)\b"),
    "verif": re.compile(r"\bverif(?:y|ies|ied|ying|ication(?:s)?)\b"),
    "publish": re.compile(r"\b(?:publish(?:es|ed|ing)?|publication(?:s)?)\b"),
}


def _result(probe: Probe, index: int, text: str) -> RetrievalResult:
    return RetrievalResult(
        id=f"{probe.name}-chunk-{index}",
        text=text,
        score=1.0 - index / 100,
        relevance=0.9,
        metadata={
            "document_id": f"{probe.name}-document-{index}",
            "document_version_id": f"version-{index}",
            "document_title": f"Controlled source {index}",
        },
    )


def _results(probe: Probe) -> tuple[RetrievalResult, ...]:
    return tuple(_result(probe, index, text) for index, text in enumerate(probe.passages, 1))


def _request(probe: Probe) -> HarborChatRequest:
    results = _results(probe)
    if not results:
        return HarborChatRequest(
            messages=(HarborChatMessage.user(prompt_text(probe.question, ())),),
            sensitive=True,
            metadata=HarborChatMetadata(
                tenant_id="QUALITY_PROBE",
                conversation_id=f"probe-{probe.name}",
            ),
        )
    response = RetrievalResponse(
        request_id=f"probe-{probe.name}",
        lane=RetrievalLane.HYBRID,
        results=results,
        diagnostics={},
    )
    evidence = ChatEvidence.prepare(
        response,
        query=probe.question,
        history=(),
        max_bytes=32_000,
        overlay=False,
    )
    return HarborChatRequest(
        messages=(HarborChatMessage.user(evidence.prompt),),
        sensitive=True,
        metadata=HarborChatMetadata(
            tenant_id="QUALITY_PROBE",
            conversation_id=f"probe-{probe.name}",
            chunk_ids=tuple(result.id for result in evidence.passages),
        ),
    )


def _scope_request(probe: ScopeProbe) -> HarborChatRequest:
    return HarborChatRequest(
        messages=(
            HarborChatMessage.user(
                json.dumps({"query": probe.question, "recent_history": probe.recent_history})
            ),
        ),
        max_tokens=512,
        sensitive=True,
        metadata=HarborChatMetadata(
            tenant_id="QUALITY_PROBE",
            conversation_id=f"probe-{probe.name}",
        ),
    )


def _scope_assessment(probe: ScopeProbe, answer: str, finish_reason: str) -> dict[str, object]:
    try:
        decision = json.loads(answer)
    except json.JSONDecodeError:
        decision = None
    scope = decision.get("scope") if isinstance(decision, dict) else None
    passed = (
        finish_reason == "stop"
        and isinstance(decision, dict)
        and set(decision) == {"scope"}
        and scope == probe.expected
    )
    return {
        "name": probe.name,
        "passed": passed,
        "expected": probe.expected,
        "actual": scope,
        "finish_reason": finish_reason,
    }


def _assessment(probe: Probe, answer: str) -> dict[str, object]:
    normalized = answer.casefold().replace("’", "'")
    results = _results(probe)
    cited_chunks = {result.id for result in cited_results(answer, results)}
    cited = {index for index, result in enumerate(results, 1) if result.id in cited_chunks}
    valid_markers = {citation_marker(index, result) for index, result in enumerate(results, 1)}
    answer_markers = tuple(
        match.group(0)
        for match in re.finditer(r"\[Source\s+\d+[^\]]*\]", answer, flags=re.IGNORECASE)
    )
    numbered_marker_count = len(
        re.findall(r"\[Source\s+\d+(?=[\s:\]—-])", answer, flags=re.IGNORECASE)
    )
    required_terms = all(_term_present(term, normalized) for term in probe.required_terms)
    checks = {
        "required_terms": required_terms,
        "required_sources": set(probe.required_sources) <= cited,
        "allowed_sources": (probe.allowed_sources is None or cited <= set(probe.allowed_sources)),
        "forbidden_terms": not any(term.casefold() in normalized for term in probe.forbidden_terms),
        "citation_range": all(1 <= source <= len(probe.passages) for source in cited),
        "no_fabricated_markers": len(answer_markers) == numbered_marker_count
        and all(marker in valid_markers for marker in answer_markers),
        "abstention": not probe.abstention_required
        or (
            not cited
            and any(
                phrase in normalized
                for phrase in (
                    "cannot answer",
                    "can't answer",
                    "cannot determine",
                    "can't determine",
                    "no sources",
                    "no retrieved sources",
                    "do not have sources",
                    "don't have sources",
                )
            )
        ),
    }
    return {
        "name": probe.name,
        "passed": all(checks.values()),
        "checks": checks,
        "cited_sources": sorted(cited),
        "answer": answer,
    }


def _term_present(term: str, normalized: str) -> bool:
    if term == "2026-09-01":
        return term in normalized or any(
            date in normalized
            for date in ("september 1, 2026", "1 september 2026", "september 1st, 2026")
        )
    pattern = _TERM_PATTERNS.get(term)
    return (
        bool(pattern.search(normalized)) if pattern is not None else term.casefold() in normalized
    )


async def _run() -> int:
    service = RuntimeChatService(RuntimeSettings())
    reports: list[dict[str, object]] = []
    scope_reports: list[dict[str, object]] = []
    try:
        for probe in PROBES:
            response = await service.complete(_request(probe), prompt=ChatPrompt.DEFAULT)
            reports.append(_assessment(probe, response.text))
        for probe in SCOPE_PROBES:
            response = await service.complete(_scope_request(probe), prompt=ChatPrompt.QUERY_GATE)
            scope_reports.append(_scope_assessment(probe, response.text, response.finish_reason))
    finally:
        await service.aclose()
    passed = all(report["passed"] is True for report in (*reports, *scope_reports))
    print(
        json.dumps(
            {"passed": passed, "probes": reports, "scope_probes": scope_reports},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
