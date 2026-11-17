# HarborRAG website assets

Find the static files the documentation site uses and regenerate its favicons. The build
copies everything in this directory except Python files into `site/assets/`.

## Visual assets

- `logos/`: the existing navigation and social artwork.
- `icons/`: the existing scalable marks and documentation icons; `icons/library/` holds
  the small UI icons.
- `favicons/`: browser, Apple touch and Android favicon variants.
- `site.webmanifest`: the web app manifest that points at the favicons.
- `css/docs.css`: documentation content and code-block styling.
- `js/search.js`: client-side documentation search.
- `js/codeux.js`: code-block interaction helpers.
- `js/mermaid-init.js`: Mermaid diagram initialization.

The existing layout and visual assets are kept on purpose until the company frontend team
replaces them. Documentation and release work should not add a parallel visual system.

## Regenerate favicons

`generate_favicons.py` renders `icons/HarborRAG-icon-static.svg` into the checked-in PNG
sizes, `favicon.ico`, the Apple touch icon and the two Android icons in `favicons/`. It
needs `cairosvg` and `pillow`, which the workspace environment provides:

```bash
uv run python website/assets/generate_favicons.py
```

After changing any asset, build the site as described in the
[website README](../README.md#build-the-site) and inspect both the desktop and the mobile
layout. New assets must not contain personal attribution, predecessor product names or
unsupported capability claims; the retained compatibility artwork is the temporary
exception until the company frontend team replaces it.
