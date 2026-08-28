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


def test_page_referenced_by_title_resolves_to_its_page_id() -> None:
    """The measured defect: 173 of 173 in-body page refs used ri:content-title and the
    parser read only ri:content-id, so every link a reader sees was dropped."""

    html = '<p>See <ac:link><ri:page ri:content-title="Deploy Runbook"/></ac:link></p>'
    calls: list[tuple[str, str]] = []

    def resolve(space: str, title: str) -> str | None:
        calls.append((space, title))
        return "8891" if title == "Deploy Runbook" else None

    relations = ConfluenceSourceRelationResolver().relations(
        html,
        current_space="ENG",
        source_version="7",
        resolve_title=resolve,
    )

    assert [(relation["predicate"], relation["target_id"]) for relation in relations] == [
        ("links_to", "confluence://ENG/8891")
    ]
    assert calls == [("ENG", "Deploy Runbook")]


def test_an_unresolvable_title_yields_no_edge_and_no_title_keyed_node() -> None:
    """A title is not an identity. The graph keys pages by page_id, so a title-keyed
    target would mint a node that can never converge with the real page."""

    html = '<p><ac:link><ri:page ri:content-title="Somewhere Else" ri:space-key="OTHER"/></ac:link></p>'

    relations = ConfluenceSourceRelationResolver().relations(
        html,
        current_space="ENG",
        source_version="7",
        resolve_title=lambda space, title: None,
    )

    assert relations == []


def test_titles_are_ignored_when_no_resolver_is_supplied() -> None:
    html = '<p><ac:link><ri:page ri:content-title="Deploy Runbook"/></ac:link></p>'

    assert (
        ConfluenceSourceRelationResolver().relations(html, current_space="ENG", source_version="7")
        == []
    )


def test_a_title_inside_an_include_macro_stays_a_transclusion() -> None:
    html = (
        '<ac:structured-macro ac:name="include">'
        '<ri:page ri:content-title="Shared Header"/>'
        "</ac:structured-macro>"
    )

    relations = ConfluenceSourceRelationResolver().relations(
        html,
        current_space="ENG",
        source_version="7",
        resolve_title=lambda space, title: "5150",
    )

    assert [(relation["predicate"], relation["target_id"]) for relation in relations] == [
        ("includes", "confluence://ENG/5150")
    ]
