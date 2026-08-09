from __future__ import annotations

import pytest
from confluence_fixtures import heading, page_input, paragraph, tabs_container, tabs_page, walk

from harborrag_adapters.connectors.confluence.normalization import (
    ConfluencePageNormalizer,
)
from harborrag_core.domain import DocumentBlockKind

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_adf_native_tabs_keep_each_tabs_content_independently_grouped_and_titled():
    """ADF's native tabsContainer/tabsPage (the modern Confluence Cloud
    editor's Tabs feature) used to have no _KIND_MAP entry, so both tabs'
    content fell through to generic "unsupported" handling: tab_path stayed
    empty for every paragraph and the tab titles were dropped entirely,
    leaving two tabs' content flattened together indistinguishably."""
    page = page_input(
        [
            heading(1, "Overview"),
            tabs_container(
                tabs_page("Setup", [paragraph("Setup instructions here.")]),
                tabs_page("Troubleshooting", [paragraph("Troubleshooting steps here.")]),
            ),
        ]
    )

    document = ConfluencePageNormalizer().normalize(page)
    blocks = tuple(walk(document.blocks[0]))

    tab_blocks = [block for block in blocks if block.kind == DocumentBlockKind.TAB]
    assert [block.title for block in tab_blocks] == ["Setup", "Troubleshooting"]

    paragraphs = [block for block in blocks if block.kind == DocumentBlockKind.PARAGRAPH]
    assert [(block.text, block.tab_path) for block in paragraphs] == [
        ("Setup instructions here.", ("Setup",)),
        ("Troubleshooting steps here.", ("Troubleshooting",)),
    ]
    assert any(block.kind == DocumentBlockKind.TAB_SET for block in blocks)


def test_heading_hierarchy_uses_nearest_shallower_heading_without_phantom_sections():
    page = page_input(
        [
            heading(1, "Deployment"),
            paragraph("overview"),
            heading(4, "Production"),
            paragraph("limits"),
            heading(2, "Environments"),
        ]
    )

    first = ConfluencePageNormalizer().normalize(page)
    second = ConfluencePageNormalizer().normalize(page)
    blocks = tuple(walk(first.blocks[0]))
    sections = [block for block in blocks if block.kind == DocumentBlockKind.SECTION]

    assert [section.section_path for section in sections] == [
        ("Deployment",),
        ("Deployment", "Production"),
        ("Deployment", "Environments"),
    ]
    assert sections[1].parent_section_id == sections[0].section_id
    assert [block.block_id for block in blocks] == [
        block.block_id for block in walk(second.blocks[0])
    ]
    assert [element.content for element in first.content] == [
        "Deployment",
        "overview",
        "Production",
        "limits",
        "Environments",
    ]


def test_repeated_heading_titles_get_disambiguated_unique_identities():
    page = page_input(
        [
            heading(1, "Overview"),
            paragraph("first"),
            heading(1, "Overview"),
            paragraph("second"),
            heading(1, "Overview"),
            paragraph("third"),
        ]
    )

    first = ConfluencePageNormalizer().normalize(page)
    second = ConfluencePageNormalizer().normalize(page)
    blocks = tuple(walk(first.blocks[0]))
    sections = [block for block in blocks if block.kind == DocumentBlockKind.SECTION]

    assert [section.section_path for section in sections] == [
        ("Overview",),
        ("Overview (2)",),
        ("Overview (3)",),
    ]
    # The disambiguating suffix only decorates the internal identity path;
    # the section's displayed title and heading text stay untouched.
    assert [section.title for section in sections] == ["Overview", "Overview", "Overview"]
    headings = [block for block in blocks if block.kind == DocumentBlockKind.HEADING]
    assert [block.text for block in headings] == ["Overview", "Overview", "Overview"]

    block_ids = [block.block_id for block in blocks]
    assert len(set(block_ids)) == len(block_ids)
    # Deterministic across independent normalizer runs over unchanged content.
    assert block_ids == [block.block_id for block in walk(second.blocks[0])]


def test_first_occurrence_of_a_repeated_heading_keeps_its_unsuffixed_identity():
    unique_page = page_input([heading(1, "Overview"), paragraph("only")])
    repeated_page = page_input(
        [heading(1, "Overview"), paragraph("first"), heading(1, "Overview"), paragraph("second")]
    )

    unique_result = ConfluencePageNormalizer().normalize(unique_page)
    repeated_result = ConfluencePageNormalizer().normalize(repeated_page)

    unique_section = next(
        block for block in walk(unique_result.blocks[0]) if block.kind == DocumentBlockKind.SECTION
    )
    first_repeated_section = next(
        block
        for block in walk(repeated_result.blocks[0])
        if block.kind == DocumentBlockKind.SECTION
    )

    assert unique_section.block_id == first_repeated_section.block_id


def test_tabs_expands_panels_and_unknown_macros_preserve_context_and_visible_body():
    tab = {
        "type": "bodiedExtension",
        "attrs": {"extensionKey": "tab", "parameters": {"title": "Production"}},
        "content": [heading(3, "Resource Limits"), paragraph("CPU: 2")],
    }
    tabs = {
        "type": "bodiedExtension",
        "attrs": {"extensionKey": "tabs", "parameters": {"title": "Environment"}},
        "content": [tab],
    }
    unknown = {
        "type": "bodiedExtension",
        "attrs": {
            "extensionKey": "vendor-secret-widget",
            "parameters": {"title": "Widget", "token": "do-not-store"},
        },
        "content": [paragraph("visible fallback body")],
    }
    page = page_input(
        [
            heading(1, "Deploy"),
            tabs,
            {
                "type": "expand",
                "attrs": {"title": "Advanced"},
                "content": [
                    {"type": "panel", "attrs": {"panelType": "warning"}, "content": [unknown]}
                ],
            },
        ]
    )

    document = ConfluencePageNormalizer().normalize(page)
    blocks = tuple(walk(document.blocks[0]))
    by_kind = {
        kind: [block for block in blocks if block.kind == kind] for kind in DocumentBlockKind
    }
    evidence = next(block for block in blocks if block.text == "CPU: 2")
    unsupported = by_kind[DocumentBlockKind.UNSUPPORTED][0]

    assert by_kind[DocumentBlockKind.TAB_SET][0].title == "Environment"
    assert by_kind[DocumentBlockKind.TAB][0].title == "Production"
    assert evidence.tab_path == ("Production",)
    assert evidence.attributes["tab_set_id"]
    assert evidence.attributes["tab_id"]
    assert "Advanced" in unsupported.container_path
    assert unsupported.attributes["parameters"] == {"title": "Widget"}
    assert document.warnings == ("unsupported Confluence macro preserved: vendor-secret-widget",)
    # The fallback body is a nested paragraph, not duplicated onto the
    # unsupported block's own aggregated text.
    assert unsupported.text is None
    fallback = next(block for block in blocks if block.text == "visible fallback body")
    assert fallback.kind == DocumentBlockKind.PARAGRAPH
    assert fallback.parent_block_id == unsupported.block_id
    assert "visible fallback body" in [element.content for element in document.content]


def test_links_media_and_unknown_empty_macro_are_recoverable_without_signed_urls():
    page = page_input(
        [
            paragraph(
                "Child page",
                marks=[
                    {
                        "type": "link",
                        "attrs": {
                            "href": "https://example.atlassian.net/wiki/spaces/ENG/pages/99?token=x"
                        },
                    }
                ],
            ),
            {
                "type": "mediaSingle",
                "content": [
                    {
                        "type": "media",
                        "attrs": {
                            "id": "attachment-1",
                            "type": "file",
                            "alt": "limits.csv",
                        },
                    }
                ],
            },
            {
                "type": "extension",
                "attrs": {"extensionKey": "empty-vendor-macro", "parameters": {}},
            },
        ]
    )

    document = ConfluencePageNormalizer().normalize(page)
    blocks = tuple(walk(document.blocks[0]))
    link = next(block for block in blocks if block.kind == DocumentBlockKind.LINK_REFERENCE)
    media = next(block for block in blocks if block.kind == DocumentBlockKind.MEDIA_REFERENCE)
    unsupported = next(block for block in blocks if block.kind == DocumentBlockKind.UNSUPPORTED)

    assert link.attributes["target_page_id"] == "99"
    assert "token=" not in link.attributes["target_url"]
    assert media.attributes["attachment_id"] == "attachment-1"
    assert media.attributes["filename"] == "limits.csv"
    assert unsupported.text is None
    assert {relation.predicate for relation in document.relations} == {
        "links_to",
        "has_attachment",
    }


def test_nested_paragraphs_in_quotes_and_list_items_are_not_duplicated():
    page = page_input(
        [
            {"type": "blockquote", "content": [paragraph("Quoted text")]},
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [paragraph("Item text")]},
                ],
            },
        ]
    )

    document = ConfluencePageNormalizer().normalize(page)
    blocks = tuple(walk(document.blocks[0]))
    quote = next(block for block in blocks if block.kind == DocumentBlockKind.QUOTE)
    list_item = next(block for block in blocks if block.kind == DocumentBlockKind.LIST_ITEM)

    assert quote.text is None
    assert list_item.text is None
    assert [element.content for element in document.content] == ["Quoted text", "Item text"]


def test_link_nested_inside_a_quote_paragraph_is_not_duplicated():
    page = page_input(
        [
            {
                "type": "blockquote",
                "content": [
                    paragraph(
                        "See docs",
                        marks=[
                            {
                                "type": "link",
                                "attrs": {
                                    "href": "https://example.atlassian.net/wiki/spaces/ENG/pages/99"
                                },
                            }
                        ],
                    )
                ],
            },
        ]
    )

    document = ConfluencePageNormalizer().normalize(page)
    blocks = tuple(walk(document.blocks[0]))
    links = [block for block in blocks if block.kind == DocumentBlockKind.LINK_REFERENCE]
    paragraph_block = next(block for block in blocks if block.kind == DocumentBlockKind.PARAGRAPH)

    assert len(links) == 1
    assert links[0].parent_block_id == paragraph_block.block_id
    assert len(document.relations) == 1
    relation = document.relations[0]
    assert relation.predicate == "links_to"
    assert relation.target_id == "99"
    assert relation.target_type == "document"


def test_nested_macro_of_the_same_kind_is_not_duplicated():
    # A raw text run as direct macro content (not wrapped in a paragraph)
    # is the case that exposes same-kind nesting bugs: a paragraph child
    # would already be excluded regardless of the nested macro's own kind,
    # masking a regression that only shows up when nothing else shields it.
    inner = {
        "type": "extension",
        "attrs": {"extensionKey": "vendor-inner", "parameters": {}},
        "content": [{"type": "text", "text": "Inner raw text"}],
    }
    outer = {
        "type": "bodiedExtension",
        "attrs": {"extensionKey": "vendor-outer", "parameters": {}},
        "content": [inner],
    }
    page = page_input([outer])

    document = ConfluencePageNormalizer().normalize(page)
    blocks = tuple(walk(document.blocks[0]))
    unsupported = [block for block in blocks if block.kind == DocumentBlockKind.UNSUPPORTED]

    assert len(unsupported) == 2
    texts = [block.text for block in unsupported]
    assert texts.count("Inner raw text") == 1
    assert None in texts
