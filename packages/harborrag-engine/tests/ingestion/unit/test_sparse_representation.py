from __future__ import annotations

from harborrag_core.ingestion import SparseEncoderProfile
from harborrag_engine.ingestion import BM25SparseEncoder


def profile() -> SparseEncoderProfile:
    return SparseEncoderProfile(
        profile_id="bm25-v1",
        fixed_avg_len=100,
        stopwords=("the", "and"),
        stopword_profile="english-minimal-v1",
    )


def test_sparse_encoding_is_deterministic_and_sorted() -> None:
    encoder = BM25SparseEncoder(profile())

    first = encoder.encode("Worker timeout timeout and retry")
    second = encoder.encode("Worker timeout timeout and retry")

    assert first == second
    assert first.vector.indices == sorted(first.vector.indices)
    assert len(first.vector.indices) == 3
    assert first.token_count == 4


def test_ingestion_and_query_use_the_same_profile_and_token_policy() -> None:
    ingestion_encoder = BM25SparseEncoder(profile())
    query_encoder = BM25SparseEncoder(profile())

    indexed = ingestion_encoder.encode("Deployment worker timeout")
    query = query_encoder.encode("worker timeout")

    assert query.profile_id == indexed.profile_id
    assert set(query.vector.indices).issubset(indexed.vector.indices)


def test_repeated_terms_receive_more_weight_than_single_terms() -> None:
    encoder = BM25SparseEncoder(profile())
    single = encoder.encode("timeout")
    repeated = encoder.encode("timeout timeout timeout")

    assert repeated.vector.indices == single.vector.indices
    assert repeated.vector.values[0] > single.vector.values[0]


def test_sparse_encoding_preserves_jira_identifier_as_one_exact_token() -> None:
    encoder = BM25SparseEncoder(profile())

    assert encoder.tokenize("Investigate HARBOR-142") == ("investigate", "harbor-142")
    assert (
        encoder.encode("HARBOR-142").vector.indices == encoder.encode("harbor-142").vector.indices
    )


def test_unicode_tokenizer_preserves_non_ascii_words() -> None:
    tokens = BM25SparseEncoder(profile()).tokenize("การปรับใช้ résumé naïve")

    assert "การปรับใช้" in tokens
    assert "résumé" in tokens


def test_sparse_encoder_emits_a_safe_lane_for_punctuation_only_text() -> None:
    encoding = BM25SparseEncoder(profile()).encode("---")

    assert encoding.token_count == 0
    assert len(encoding.vector.indices) == 1
    assert len(encoding.vector.values) == 1
