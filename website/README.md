# HarborRAG documentation website

Build, preview and check the HarborRAG documentation site, and learn which Markdown files
reach it. This directory holds the static-site builder. The repository content stays
authoritative:

- `docs/TOC.md` defines the documentation sections, their order and curated links.
- `docs/**/*.md` supplies the task and architecture guides.
- `packages/*/pyproject.toml` and `packages/*/README.md` supply package metadata and
  reference pages.
- The root `pyproject.toml` supplies the product description, version and repository
  URLs.

The templates provide presentation only. Do not copy technical capability, installation,
package or navigation data into a template when it can be linked or generated from these
sources.

## Presentation boundary

The Bootstrap-based layout is kept on purpose while the frontend team builds its
replacement. Documentation changes focus on the generated pages, release metadata, link
correctness and publication safety; do not introduce a parallel redesign or a new visual
component system.

Capability statements must stay backed by the root README and
`docs/getting-started/what-is-harborrag.md`. Do not publish customer logos, adoption
numbers, performance claims or production-readiness claims without a repository-owned
source of truth.

## Build the site

Install the workspace once, from the repository root:

```bash
uv sync --all-packages --all-extras
```

Then build **one** of two ways. They are alternatives, not successive steps: the only
difference is the origin the generated pages claim to be served from.

| | Option A: preview locally | Option B: build for a domain |
| --- | --- | --- |
| Origin | `http://127.0.0.1:8765` | The public URL the site is served from |
| Output | Served from a local port | A `site/` tree ready to deploy |
| Use when | Writing or reviewing documentation | Publishing, or checking what CI produces |

### Option A: preview on a local port

The preview uses port 8765 because the HarborRAG API of the local stack already listens on
8000. Build against the local origin, then serve the output over HTTP: directory links
and root-relative assets only behave as they do when deployed over a real server, not
over `file://`.

```bash
uv run python website/build.py \
  --output site \
  --templates website/templates \
  --base-url "" \
  --site-url http://127.0.0.1:8765

uv run python -m http.server 8765 --bind 127.0.0.1 --directory site
```

Open `http://127.0.0.1:8765/` in a browser. Keep this server running while you run the
link checker below: it crawls the served site, not the files on disk.

### Option B: build for a specific domain

Pass the public origin the site will be served from:

```bash
uv run python website/build.py \
  --output site \
  --templates website/templates \
  --base-url "" \
  --site-url https://docs.example.com
```

CI does exactly this, reading the origin from the `DOCS_SITE_URL` repository variable and
falling back to the current GitHub Pages URL. Moving the site to a new host is one
variable change rather than a search for hardcoded literals.

### `--base-url` and `--site-url` are different things

- `--base-url` is the **path prefix** for asset and navigation links. It stays empty for
  a site served from the root of its origin; set it only when the site lives under a
  sub-path.
- `--site-url` is the **absolute origin** used for canonical tags, Open Graph URLs,
  `sitemap.xml` and `robots.txt`.

They are separate inputs because a root-served site needs relative asset paths and
absolute SEO URLs at the same time. Omitting `--site-url` falls back to the published
origin and, while `--base-url` is empty, prints a warning, so a forgotten flag is visible
in the build log rather than silently wrong in the output.

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

Every package with both a `pyproject.toml` and a `README.md` is discovered
automatically. Redirects keep the former `core` and `mcp-server` package-documentation
URLs working.

## Publication boundary

Only these sources reach the website. Everything else is an in-tree developer note that
no reader of the documentation will ever see.

| Source | Published as | Audience |
| --- | --- | --- |
| `docs/**/*.md` | `/docs/**` | Everyone |
| `packages/<name>/README.md` | `/docs/packages/<name>/` and the PyPI project page | Package users |
| `packages/<name>/docs/**/*.md` | *reserved, see below* | Package users needing depth |
| `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `SECURITY.md`, `LICENSE` | `/docs/<name>.html` | Everyone |
| Any other `README.md` (under `src/`, `tests/`, `deploy/`, `examples/`, `website/`) | Not published | Contributors working in that directory |

A `packages/<name>/README.md` is only discovered when the directory is a real
distribution: the builder finds packages by their `pyproject.toml`.

Check where a change lands before writing it:

```bash
uv run python website/check_docs_publish_path.py            # whole-tree summary
uv run python website/check_docs_publish_path.py path/to/file.md
```

`--changed-from REF` classifies only the Markdown changed since `REF`. Quality Gates runs
the whole-tree report on every pull request. It is advisory: it tells you when
documentation effort is going somewhere no reader will find it, and `--strict` turns that
into a failure for the paths you name.

`packages/<name>/docs/` is the agreed home for detailed package documentation, but the
builder does not render it yet. Until it does, put reader-facing depth under `docs/` and
keep the package README as the entry point.

## Package README conventions

Each `packages/<name>/README.md` is set as `readme` in that package's `pyproject.toml`, so
it is both the site's package page and the package's **PyPI project page**. Write it for
someone who arrived from PyPI knowing nothing about the monorepo.

Keep it simple, clear and professional. A package README is an entry point, not a manual:
state what the package is, show it working, then link onward.

Use this skeleton, leaving out sections that do not apply:

```markdown
# harborrag-<name>

One sentence on what this package does and who needs it.

## Install
## Quick start        # the smallest example that actually runs
## What it provides   # the public surface, in a short list
## Advanced usage     # a link out, not the content itself
## Contributing       # a link to the root CONTRIBUTING.md
## License
```

Rules that follow from the PyPI constraint:

- **Absolute links only.** PyPI renders the README outside the repository, so relative
  links to `../../docs/...` resolve to nothing there. Link to the published site or to a
  full `https://github.com/cbtw-apac/HarborRAG/blob/main/...` URL.
- **No Sphinx roles or directives.** PyPI's renderer rejects them.
- **Self-contained.** Do not assume the reader can see sibling packages.

Rules that follow from keeping READMEs short:

- **Pop advanced features out.** When a section grows past a screen, move it to a page
  under `docs/` and leave a one-line link. Configuration matrices, tuning guides and
  provider-by-provider tables belong there, not in a README.
- **Link upstream rather than re-documenting it.** For third-party technology, a sentence
  on how HarborRAG uses it plus a link to the original project is better than a summary
  that will drift. For example:
  [liteparse](https://github.com/run-llama/liteparse),
  [Docling](https://github.com/docling-project/docling),
  [MinerU](https://github.com/opendatalab/MinerU),
  [Qdrant](https://github.com/qdrant/qdrant).
- **One heading level of nesting.** If you need `####`, the content wants its own page.

READMEs under `src/` and `tests/` are contributor notes. Keep them to orientation, such as
what lives here and which invariants hold, and move anything a user would read into the
publication boundary above.

## Add or update documentation

1. Edit or create the Markdown page under `docs/`.
2. Add its ordered entry to `docs/TOC.md` when it belongs in navigation.
3. Update the nearest package README when the information is package-specific.
4. Build the site and run the internal link checker.
5. Run the website test suite before opening a pull request.

Use relative Markdown links in source documents, except in package READMEs. The builder
translates `.md` targets to generated `.html` targets and keeps `#fragments`.

The builder enables Markdown inside HTML blocks, so a collapsible block written as
`<details markdown="1"><summary>macOS</summary> ... </details>` renders on both GitHub
and the site. Tab syntax such as `=== "Tab"` is not supported.

## Validation

Build and serve the site with [Option A](#option-a-preview-on-a-local-port), leave that
server running in another terminal, then run the checks:

```bash
uv run pytest tests/test_website_*.py tests/test_link_checker*.py
uv run python website/check_branding.py
uv run python website/check_publication.py
uv run python website/check_docs_publish_path.py
uv run python website/check_links.py --url http://127.0.0.1:8765 --depth 8
```

External URL checks are opt-in with `check_links.py --external`, because they need
network access and may be flaky. CI always validates the internal generated links,
rejects unintended predecessor branding, and verifies that private reference inputs are
ignored, untracked, unlinked and absent from the generated output.

Generated report trees are entered but not descended: `coverage/index.html` is built
here, so its links are validated, while the coverage.py output beneath it is left
unparsed. That HTML renders this builder's own source, and the string literals in it read
as links that were never meant to resolve. Pass `--no-crawl` to name a different tree, or
`--depth` to widen the crawl.

## Publishing

`.github/workflows/docs-auto.yml` rebuilds the documentation when guides, package
READMEs, website sources or the relevant workflow files change on `main`.

Building is not deploying. A push to `main` builds, verifies and link-checks the site,
then uploads it as an artifact. The `stage-pages` and `deploy` jobs are gated on a
published `harborrag-v*` release, so documentation merged to `main` goes live at the next
main-package release. Run `.github/workflows/docs-manual.yml` from `main` with
`deploy=true` to publish sooner.

Report documentation problems in the
[HarborRAG issue tracker](https://github.com/cbtw-apac/HarborRAG/issues).
