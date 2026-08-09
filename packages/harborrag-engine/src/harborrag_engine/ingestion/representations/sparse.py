from __future__ import annotations

from collections import Counter
from hashlib import sha256
from unicodedata import category, normalize

from harborrag_core.indexing import SparseVector
from harborrag_core.ingestion import SparseEncoderProfile, SparseEncoding


class BM25SparseEncoder:
    """Create deterministic Qdrant sparse vectors with collection-side IDF."""

    def __init__(self, profile: SparseEncoderProfile) -> None:
        self.profile = profile
        self._stopwords = frozenset(
            word.casefold() if profile.lowercase else word for word in profile.stopwords
        )

    def encode(self, text: str) -> SparseEncoding:
        tokens = self.tokenize(text)
        index_tokens = tokens or ("__harborrag_empty_token__",)
        term_frequencies = Counter(index_tokens)
        length = len(tokens)
        by_index: dict[int, float] = {}
        for term, frequency in term_frequencies.items():
            index = self._index(term)
            weight = self._term_frequency_weight(frequency, length)
            by_index[index] = by_index.get(index, 0.0) + weight
        indices = sorted(by_index)
        return SparseEncoding(
            profile_id=self.profile.profile_id,
            vector=SparseVector(
                indices=indices,
                values=[by_index[index] for index in indices],
            ),
            token_count=length,
        )

    def tokenize(self, text: str) -> tuple[str, ...]:
        normalized = normalize("NFC", text)
        tokens: list[str] = []
        for token in _unicode_words(normalized):
            selected = token.casefold() if self.profile.lowercase else token
            if selected not in self._stopwords:
                tokens.append(selected)
        return tuple(tokens)

    def _index(self, term: str) -> int:
        digest = sha256(term.encode()).digest()
        return int.from_bytes(digest[:8], "big") % self.profile.hash_space

    def _term_frequency_weight(self, frequency: int, document_length: int) -> float:
        profile = self.profile
        normalized_length = document_length / profile.fixed_avg_len
        denominator = frequency + profile.k * (1.0 - profile.b + profile.b * normalized_length)
        return frequency * (profile.k + 1.0) / denominator


def _unicode_words(text: str) -> tuple[str, ...]:
    words: list[str] = []
    current: list[str] = []
    for index, character in enumerate(text):
        if _is_word_character(character):
            current.append(character)
            continue
        has_word_after = index + 1 < len(text) and _is_word_character(text[index + 1])
        if character in {"-", "'"} and current and has_word_after:
            current.append(character)
            continue
        if current:
            words.append("".join(current))
            current.clear()
    if current:
        words.append("".join(current))
    return tuple(words)


def _is_word_character(character: str) -> bool:
    return category(character)[0] in {"L", "M", "N"}
