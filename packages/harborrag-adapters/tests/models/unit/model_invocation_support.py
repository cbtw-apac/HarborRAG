from __future__ import annotations

from typing import Any


class FakeEmbeddingInvocation:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.async_calls: list[dict[str, Any]] = []
        self.close_count = 0
        self.aclose_count = 0

    def embed(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._next()

    async def aembed(self, **kwargs: Any) -> Any:
        self.async_calls.append(kwargs)
        return self._next()

    def close(self) -> None:
        self.close_count += 1

    async def aclose(self) -> None:
        self.aclose_count += 1

    def _next(self) -> Any:
        if not self.responses:
            raise AssertionError("FakeEmbeddingInvocation has no queued response")
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeRerankInvocation:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.async_calls: list[dict[str, Any]] = []
        self.close_count = 0
        self.aclose_count = 0

    def rerank(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._next()

    async def arerank(self, **kwargs: Any) -> Any:
        self.async_calls.append(kwargs)
        return self._next()

    def close(self) -> None:
        self.close_count += 1

    async def aclose(self) -> None:
        self.aclose_count += 1

    def _next(self) -> Any:
        if not self.responses:
            raise AssertionError("FakeRerankInvocation has no queued response")
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def embedding_response(
    vectors: list[list[float] | str],
    *,
    indexes: list[int] | None = None,
    prompt_tokens: int = 0,
    total_tokens: int | None = None,
) -> dict[str, Any]:
    return {
        "model": "embedding-model",
        "data": [
            {"index": index, "embedding": vector}
            for index, vector in zip(indexes or list(range(len(vectors))), vectors, strict=True)
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "total_tokens": prompt_tokens if total_tokens is None else total_tokens,
        },
    }


def rerank_response(
    results: list[tuple[int, float]],
    *,
    documents: dict[int, str | dict[str, Any]] | None = None,
    search_units: int = 0,
) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for index, score in results:
        item: dict[str, Any] = {"index": index, "relevance_score": score}
        if documents is not None and index in documents:
            item["document"] = documents[index]
        values.append(item)
    return {
        "id": "rerank-1",
        "results": values,
        "meta": {"billed_units": {"search_units": search_units}},
    }
