from __future__ import annotations

from harborrag_adapters.connectors.confluence.relations import (
    ConfluenceSourceRelationResolver,
)


def test_confluence_links_and_includes_are_explicit_without_inlining() -> None:
    html = """
    <p><a href="/wiki/spaces/OPS/pages/22/Runbook">Runbook</a></p>
    <ac:link><ri:page ri:content-id="33" ri:space-key="ENG"/></ac:link>
    <ac:structured-macro ac:name="include">
      <ri:page ri:content-id="44" ri:space-key="ARCH"/>
    </ac:structured-macro>
    """

    relations = ConfluenceSourceRelationResolver().relations(
        html,
        current_space="ENG",
        source_version="7",
    )

    assert {(relation["predicate"], relation["target_id"]) for relation in relations} == {
        ("links_to", "confluence://OPS/22"),
        ("links_to", "confluence://ENG/33"),
        ("includes", "confluence://ARCH/44"),
    }
