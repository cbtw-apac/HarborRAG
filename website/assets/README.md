# HarborRAG website assets

Assets in this directory are copied into `site/assets/` during a documentation build.

## Visual assets

- `logos/` — the existing navigation and social artwork.
- `icons/` — the existing scalable marks and documentation icons.
- `favicons/` — browser, Apple touch, and Android favicon variants.
- `css/docs.css` — documentation content and code-block styling.
- `js/search.js` — client-side documentation search.
- `js/codeux.js` — code-block interaction helpers.
- `js/mermaid-init.js` — Mermaid diagram initialization.

The existing layout and visual assets are intentionally retained for the
company frontend team to replace. Documentation and release work should not add
a parallel visual system.

## Regenerate favicons

The favicon script renders the existing static SVG into the checked-in PNG and ICO sizes:

```bash
uv run python website/assets/generate_favicons.py
```

Run the website build after changing any asset and inspect both desktop and
mobile layouts. New assets must not contain personal attribution, predecessor
product names, or unsupported capability claims; the retained compatibility
artwork is the temporary exception until the company frontend team replaces it.
