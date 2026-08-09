# Confluence canonical normalization

`ConfluencePageNormalizer` is a pure transformation from a safe
`ConfluencePageInput` to the existing core `Document` contract. It prefers ADF,
then storage format, then rendered HTML, and records the selected representation
and recoverable warnings.

The canonical block tree preserves headings, sections, tabs, expands, panels,
macros, links, media references, and table references. Skipped heading levels
attach to the nearest preceding shallower heading without creating synthetic
sections. Tab titles participate in section identity so equal headings in
sibling tabs remain independent.

Unknown macros retain their safe key, identifier, filtered display parameters,
visible children, and a warning. Credential-like parameters and signed URL
queries are discarded.

Tables are retained separately as immutable `TableArtifact` values. Source
cells preserve spans, while the logical grid marks inherited merged-cell slots.
Nested tables receive independent identities and a parent-cell locator.
