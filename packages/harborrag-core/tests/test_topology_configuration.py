import pytest

from harborrag_core.topology.config import parse_indexing_configuration


def test_nested_main_switch_is_default_off_and_prohibition_wins():
    assert not parse_indexing_configuration('{"tenant_id":"t","indexing":{}}').serves_enrichment
    config = parse_indexing_configuration(
        '{"tenant_id":"t","indexing":{"llm":{"enabled":true,"prohibited":true}}}'
    )
    assert config.enabled and not config.serves_enrichment


def test_nested_and_flat_operator_formats_resolve_to_same_canonical_policy():
    assert parse_indexing_configuration(
        '{"tenant_id":"t","enabled":true}'
    ) == parse_indexing_configuration('{"tenant_id":"t","indexing":{"llm":{"enabled":true}}}')


def test_unknown_nested_options_are_rejected_not_silently_ignored():
    with pytest.raises(ValueError):
        parse_indexing_configuration('{"tenant_id":"t","indexing":{"llm":{"enable":true}}}')
