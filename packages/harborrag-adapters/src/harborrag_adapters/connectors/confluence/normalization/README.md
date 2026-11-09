# Confluence canonical normalization

This package turns one Confluence page into HarborRAG's canonical `Document`, keeping
the page's structure, macros and tables. Read it before you change how Confluence
pages are represented; the Confluence connector's `document_transform.py` is its only
caller.

## Entry point

`ConfluencePageNormalizer.normalize(page)` in `page.py` is a pure transformation from a
`ConfluencePageInput` (`schemas.py`) to the core `Document` contract. It tries the
page's body representations in this order, moving on when one is missing or, for ADF,
fails to parse:

1. ADF, the Atlassian Document Format (`adf.py`).
2. Storage format (`markup.py`).
3. Rendered HTML (`markup.py`).

The document's provenance records the representation used (`body_representation`) and
any recoverable warnings (`parser_warnings`).

## Page structure

The canonical block tree keeps headings, sections, tabs, expands, panels, macros,
links, media references and table references (`hierarchy.py`).

- A heading that skips a level attaches to the nearest preceding shallower heading.
  No synthetic section is created for the missing level.
- Tab titles are part of a section's identity, so equal headings in sibling tabs stay
  separate sections.

## Macros and includes

`ConfluenceMacroHandlerRegistry` (`macros.py`) picks a handler for each macro key.

- An unknown macro keeps its safe key, identifier, filtered display parameters and
  visible children, and adds a warning.
- Only allowlisted display parameters are kept. A parameter whose name looks like a
  credential, such as `token`, `password` or `signature`, is dropped, and a URL value
  loses its query string and fragment, which removes signed URL queries.
- Native include macros and the common `localtabgroup` and `localtab` aliases keep
  their navigation structure.
- An include becomes a reference, not recursively inlined content. This holds for
  title-only targets that expose no stable page ID too (`include_handlers.py`).
- A one-cell table that holds only an include is treated as a layout wrapper, not a
  data table.

## Tables

Tables are kept separately as immutable `TableArtifact` values from `harborrag-core`
(`tables.py`).

- Source cells keep their row and column spans, while the logical grid marks the slots
  a merged cell covers.
- A nested table gets its own identity and a locator for the parent cell that holds it.
