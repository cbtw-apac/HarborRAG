from __future__ import annotations

from types import SimpleNamespace

import confluence
import pytest

from harborrag_core.domain.raw_document import RawDocument

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _document(storage: str, *, export_view: str | None = None) -> RawDocument:
    raw = None
    if export_view is not None:
        raw = {
            "body": {
                "storage": {"value": storage},
                "export_view": {"value": export_view},
            }
        }
    return RawDocument(
        id="confluence://ENG/42",
        source="https://example.invalid/wiki/spaces/ENG/pages/42",
        content=storage,
        content_type="text/html",
        metadata={"title": "Guide"},
        raw=raw,
    )


def _render(document: RawDocument, *, markdown: bool = True) -> str:
    record = SimpleNamespace(id=document.id)
    return confluence._render_confluence_output(record, document, markdown=markdown)


def test_ordinary_page_output_is_unchanged_when_export_view_is_available() -> None:
    storage = "<h1>Guide</h1><p>Storage body</p>"
    without_export = _render(_document(storage))
    with_unused_export = _render(
        _document(storage, export_view="<h1>Guide</h1><p>Rendered body</p>")
    )

    assert with_unused_export == without_export
    assert "Storage body" in with_unused_export
    assert "body_preview" not in with_unused_export


def test_render_dependent_page_uses_sanitized_existing_export_view() -> None:
    storage = """
    <ac:structured-macro ac:name="include">
      <ac:parameter ac:name=""><ri:page ri:content-title="Child" /></ac:parameter>
    </ac:structured-macro>
    """
    rendered = """
    <style>.tabs { color: red; }</style>
    <script>alert('no')</script>
    <h2>Expanded child</h2>
    <table><tr><th>Service</th><th>CPU</th></tr><tr><td>api</td><td>4</td></tr></table>
    """

    output = _render(_document(storage, export_view=rendered))

    assert "- **body_preview**: `export_view_markdown`" in output
    assert "## Expanded child" in output
    assert "| Service | CPU |" in output
    assert "| api | 4 |" in output
    assert "<table>" not in output
    assert "<ac:structured-macro" not in output
    assert "<style" not in output
    assert "<script" not in output
    assert ".tabs { color: red; }" not in output
    assert "alert('no')" not in output


def test_render_dependent_page_keeps_storage_when_export_view_is_unavailable() -> None:
    storage = '<ac:structured-macro ac:name="localtab" />'

    output = _render(_document(storage))

    assert storage in output
    assert "body_preview" not in output


def test_plain_text_output_uses_the_same_rendered_preview_policy() -> None:
    storage = '<ac:structured-macro ac:name="include" />'
    document = _document(storage, export_view="<p>Expanded child</p>")

    assert _render(document, markdown=False) == "Expanded child"
