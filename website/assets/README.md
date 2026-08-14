# HarborRAG website assets

Assets in this directory are copied into `site/assets/` during a documentation build.

## Visual assets

- `logos/` — the existing navigation and social artwork.
- `icons/` — the existing scalable marks and documentation icons.
- `favicons/` — browser, Apple touch, and Android favicon variants.
- `css/foundation.css` — tokens, reset, shared shell, navigation, and buttons.
- `css/landing.css` — homepage hero, publication manifest, capability, and journey sections.
- `css/explorer.css` — interactive architecture explorer and system diagram.
- `css/interfaces.css` — developer examples, operator view, status, and documentation cards.
- `css/content.css` — footer and secondary documentation-page presentation.
- `css/responsive.css` — viewport, reduced-motion, and increased-contrast adaptations.
- `js/site.js` — accessible navigation, tab interfaces, reveal behavior, and scroll state; no framework or generated artwork.

The existing visual assets are intentionally retained for the company frontend team to replace. This documentation refresh does not create new artwork.

## Regenerate favicons

The favicon script renders the existing static SVG into the checked-in PNG and ICO sizes:

```bash
uv run python website/assets/generate_favicons.py
```

Run the website build after changing any asset and inspect both desktop and
mobile layouts. New assets must not contain personal attribution, predecessor
product names, or unsupported capability claims; the retained compatibility
artwork is the temporary exception until the company frontend team replaces it.
