from __future__ import annotations

import pytest
from confluence_fixtures import heading, page_input, paragraph, walk

from harborrag_adapters.connectors.confluence.normalization import (
    ConfluencePageNormalizer,
    UnsupportedConfluenceBodyError,
    filter_macro_parameters,
)
from harborrag_core.domain import DocumentBlockKind

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_body_representation_fallback_is_explicit_and_unparseable_pages_fail():
    fallback = page_input(
        adf="{broken",
        storage=None,
        rendered_html="<h1>Fallback</h1><p>Visible</p>",
    )

    document = ConfluencePageNormalizer().normalize(fallback)

    assert document.body_representation == "rendered_html"
    assert document.warnings[0].startswith("adf parsing failed")
    assert [element.content for element in document.content] == ["Fallback", "Visible"]

    with pytest.raises(UnsupportedConfluenceBodyError, match="no parseable"):
        ConfluencePageNormalizer().normalize(
            page_input(adf="{broken", storage=None, rendered_html=None)
        )


def test_macro_parameter_filter_drops_credentials_and_sanitizes_urls():
    assert filter_macro_parameters(
        {
            "title": "Safe",
            "token": "secret",
            "name": "https://example.test/path?signature=secret",
            "arbitrary": "discarded",
        }
    ) == {
        "title": "Safe",
        "name": "https://example.test/path",
    }


def test_equal_heading_paths_in_sibling_tabs_have_independent_section_identities():
    page = page_input(
        [
            {
                "type": "bodiedExtension",
                "attrs": {"extensionKey": "tabs", "parameters": {"title": "Environment"}},
                "content": [
                    {
                        "type": "bodiedExtension",
                        "attrs": {"extensionKey": "tab", "parameters": {"title": tab_title}},
                        "content": [heading(2, "Limits"), paragraph(f"{tab_title} values")],
                    }
                    for tab_title in ("Development", "Production")
                ],
            }
        ]
    )

    blocks = tuple(walk(ConfluencePageNormalizer().normalize(page).blocks[0]))
    sections = [block for block in blocks if block.kind == DocumentBlockKind.SECTION]

    assert [section.section_path for section in sections] == [("Limits",), ("Limits",)]
    assert [section.tab_path for section in sections] == [
        ("Development",),
        ("Production",),
    ]
    assert sections[0].section_id != sections[1].section_id
