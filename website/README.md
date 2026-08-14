# HarborRAG documentation website

This directory contains the static-site builder for HarborRAG. Repository content remains authoritative:

- `docs/TOC.md` defines documentation sections, order, and curated links.
- `docs/**/*.md` supplies task and architecture guides.
- `packages/*/pyproject.toml` and `packages/*/README.md` supply package metadata and reference pages.
- the root `pyproject.toml` supplies the product description, version, and repository URLs.

The templates provide presentation only. Do not copy technical capability, installation, package, or navigation data into a template when it can be linked or generated from these sources.

## Public launch story

The public homepage is organized around the questions a new open-source user
needs quick answers to:

1. **What outcome does HarborRAG create?** Turn scattered engineering
   knowledge into governed, grounded answers.
2. **How does it work?** Connect, understand, orchestrate, and retrieve.
3. **What can I use with it?** Sources, model families, storage backends, and
   operator interfaces are presented as one integration surface.
4. **Why trust the architecture?** The publication manifest, interactive
   ingestion/retrieval explorer, and package diagram make authority, evidence,
   projection, provider, and domain boundaries visible.
5. **What works today?** HarborRAG 2.0.0 and its Alpha readiness boundary are
   disclosed together, including infrastructure the operator must still own.
6. **How do I start or contribute?** Every major section routes to a real guide,
   command, repository page, or issue path.

Capability statements must remain evidence-backed by the root README and
`docs/getting-started/what-is-harborrag.md`. Do not publish customer logos,
adoption numbers, performance claims, or production-readiness claims without a
repository-owned source of truth.

The presentation layer is intentionally modular: `foundation.css` owns shared
tokens and shell styles, while landing, explorer, interface, content, and
responsive concerns live in focused stylesheets under `website/assets/css/`.
Keep new rules with the feature that owns them instead of rebuilding a single
site-wide stylesheet.

## Build locally

From the repository root:

```bash
uv sync --all-packages --all-extras
uv run python website/build.py \
  --output site \
  --templates website/templates \
  --base-url ""
```

Serve the output over HTTP so directory links and root-relative assets behave like deployment:

```bash
uv run python -m http.server 8000 --bind 127.0.0.1 --directory site
```

Open <http://127.0.0.1:8000/>.

For a deployment with an absolute public origin, pass it explicitly:

```bash
uv run python website/build.py \
  --output site \
  --templates website/templates \
  --base-url https://cbtw-apac.github.io/HarborRAG
```

## Generated structure

The builder creates:

```text
site/
├── index.html
├── docs/
│   ├── index.html
│   ├── getting-started/
│   ├── users/
│   ├── developers/
│   └── packages/<package-name>/
├── coverage/
├── assets/
├── project-info.json
├── sitemap.xml
└── robots.txt
```

Every current package with both `pyproject.toml` and `README.md` is discovered automatically. Compatibility redirects preserve the former `core`, `mcp-server`, and predecessor package-documentation URLs.

## Add or update documentation

1. Edit or create the Markdown page under `docs/`.
2. Add its ordered entry to `docs/TOC.md` when it belongs in navigation.
3. Update the nearest package README when the information is package-specific.
4. Build the site and run the internal link checker.
5. Run the website test suite before opening a pull request.

Use relative Markdown links in source documents. The builder translates `.md` targets to generated `.html` targets and preserves fragments.

## Validation

Keep the local HTTP server from the build instructions running in another
terminal while executing the link check below.

```bash
uv run pytest tests/test_website_*.py tests/test_link_checker*.py
uv run python website/check_branding.py
uv run python website/check_publication.py
uv run python website/check_links.py --url http://127.0.0.1:8000 --depth 8
```

External URL checks are opt-in because they require network access and may be
flaky. CI always validates internal generated links, rejects unintended
predecessor branding, and verifies that private reference inputs are ignored,
untracked, unlinked, and absent from generated output.

## Publishing

`.github/workflows/docs-auto.yml` rebuilds the documentation when guides, package READMEs, website sources, or relevant workflow files change. Release deployments publish the validated `site/` artifact to GitHub Pages. `.github/workflows/docs-manual.yml` provides the equivalent manual build/deploy path.

Report documentation problems in the [HarborRAG issue tracker](https://github.com/cbtw-apac/HarborRAG/issues).
