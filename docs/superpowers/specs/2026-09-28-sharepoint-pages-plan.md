# SharePoint pages support: implementation plan

Branch: `feat/sharepoint_connector`
Date: 2026-09-28

## Goal

The SharePoint connector only reads **files** in document libraries. It skips
SharePoint **pages** (modern site pages), which is where many teams keep their
written knowledge. Add page support so that pages are discovered, loaded as
HTML, and shown in the knowledge graph.

When you're done, a connection with pages turned on should ingest both the files
and the pages of a site.

## Before you start: read these

Spend some time here first. Most of the work is following patterns that already
exist.

1. [`connectors/base.py`](../../../packages/harborrag-adapters/src/harborrag_adapters/connectors/base.py):
   the connector contract. Understand `discover()` and `load()`.
2. [`connectors/sharepoint/`](../../../packages/harborrag-adapters/src/harborrag_adapters/connectors/sharepoint/):
   the connector you are extending. Read `connector.py`, then `drive.py`, then
   `mappers.py`.
3. `ConfluenceConnector.load()` in
   [`confluence/connector.py`](../../../packages/harborrag-adapters/src/harborrag_adapters/connectors/confluence/connector.py):
   an example of a connector returning a **page as HTML** (`content_type="text/html"`).
   You'll do something similar, only much simpler.
4. [`SharePointSourceProjector`](../../../packages/harborrag-engine/src/harborrag_engine/ingestion/projections/graph/file_source_projectors.py):
   how SharePoint items become graph nodes.

Microsoft docs to look up yourself: the Graph API `sitePage` resource, the
"list pages" endpoint, and `canvasLayout`.

## Out of scope

Don't do these in this branch. They're planned separately.

- Permissions / access control
- Delta sync (`/delta` API)
- Adding SharePoint to the `harborrag init` wizard (it's excluded on purpose,
  and tests check that)
- Non-text web parts (images, embedded lists, news, quick links)
- Classic (non-modern) wiki pages

---

## Step 1: Baseline smoke test

**Why:** you need to know the connector works *before* you change it, and you
need proof that pages are currently missing.

1. Get a **test SharePoint site** with non-sensitive content. Put at least one
   normal file (e.g. a `.docx`) and one **modern page** with some text in it.
2. Get app credentials from your lead (Entra ID app registration, application
   permission `Sites.Selected` for the test site, or `Sites.Read.All`).
3. Set up config by following
   [the smoke test README](../../../packages/harborrag-adapters/tests/connectors/smoke/README.md):
   - `env/.env.connector`: tenant ID, client ID, client secret, site URL
   - `config/connectors.yaml`: add a SharePoint connection (copy the commented
     block from `config/connectors.example.yaml`)
4. Run it:
   ```bash
   python packages/harborrag-adapters/tests/connectors/smoke/run.py --connector <your-connection-id>
   ```
5. Run the existing unit tests:
   ```bash
   uv run pytest packages/harborrag-adapters/tests/connectors/unit/sharepoint
   ```

**Done when:**
- The smoke test discovers and loads your file (exit code `0`).
- All SharePoint unit tests pass.
- You've confirmed your test page does **not** show up in discovery. Figure
  out *why*: is it filtered out, or never reached at all?

Write down what you saw. It goes in your PR description.

## Step 2: Explore the pages API by hand

**Why:** check that the credentials can read pages, and see real data before
writing code.

1. Using the same app credentials (Graph Explorer, `curl`, or a small throwaway
   script), call the Graph endpoints to:
   - list the pages of your test site
   - get one page with its `canvasLayout` expanded
2. Find where the page text lives in the response.
3. Save a **cleaned-up** copy of both responses (no real names, emails, or IDs)
   as test fixtures. You'll use them in steps 4 and 5.

**Done when:** you can point at the page's text in the JSON, and you have two
fixture files.

**Stop and ask your lead** if you get a `403`. That's a permission problem, not
a code problem.

## Step 3: Config switch

**Why:** pages should be opt-in, so existing connections behave exactly as
before.

1. Add an `include_pages` setting to `SharePointSiteConfig`.
2. Decide what the default should be, and write down why.
3. Think about the "Site Pages" library: it's also a drive. If both pages and
   files are on, could the same page get ingested twice? Make sure it can't.
4. Add the new setting to the commented example in `config/connectors.example.yaml`.

**Done when:** there are unit tests for the new setting, and all existing tests
still pass.

## Step 4: Discover pages

**Why:** `discover()` needs to yield one `SourceRecord` per page.

1. Create `sharepoint/pages.py`, following the style of `drive.py`.
2. List pages from Graph. Handle pagination: look at how `drive.py` follows
   `@odata.nextLink`.
3. Map each page to a `SourceRecord`. Decide:
   - the record `id` format (look at the existing `sharepoint://...` format for
     files, and make pages clearly different)
   - what goes in `metadata` (think: what will `load()` and the graph projector
     need later?)
   - what `updated_at` / version to use so unchanged pages are skipped on the
     next sync
4. Call it from `SharePointConnector.discover()` when `include_pages` is on.
   Make sure `query.limit` still works.

**Done when:** there are unit tests using your step-2 fixture that cover the
mapping, pagination, the limit, and `include_pages` being off.

## Step 5: Load a page

**Why:** `load()` must turn one page record into a `RawDocument`.

1. `load()` currently assumes every record is a file. Make it tell pages and
   files apart, and send each to the right code path.
2. For a page, fetch it with `canvasLayout` and build **one HTML string**: the
   page title as `<h1>`, then the text web parts' HTML **in the order they
   appear** on the page.
3. Return a `RawDocument` with `content_type="text/html"`. The existing HTML
   parser handles the rest, so you don't need to write a parser.
4. Decide what to do with a page that has no text at all.

**Done when:** there are unit tests using your fixture that check the HTML
output, the order of sections, and that non-text web parts are skipped without
crashing. Existing file-loading tests must still pass.

## Step 6: Graph projection

**Why:** in the knowledge graph, pages should appear as their own node type
under the site.

1. Add a `SHAREPOINT_PAGE` value next to the other `SHAREPOINT_*` values in
   [`harborrag_core/ingestion/states.py`](../../../packages/harborrag-core/src/harborrag_core/ingestion/states.py).
2. In `SharePointSourceProjector`, pages should hang off the **site** directly
   (no drive, no folders). Right now the projector assumes everything is a
   file, so you'll need a branch.
3. Add tests next to the existing ones in
   `packages/harborrag-engine/tests/ingestion/unit/test_graph_source_projectors.py`.
4. Run the graph eval CI tests:
   ```bash
   uv run pytest packages/harborrag-runtime/tests/graph_eval/unit
   ```
   If the baseline test fails, read the graph eval README first. Only
   regenerate the baseline if the diff is exactly what you expect, and ask for
   a review of that diff.

**Done when:** a page projects as Site → Page and all graph tests pass.

## Step 7: End-to-end smoke test

**Why:** unit tests use fake data. This proves it works against real SharePoint.

1. Turn `include_pages` on in your connection.
2. Update [`smoke/sharepoint.py`](../../../packages/harborrag-adapters/tests/connectors/smoke/sharepoint.py)
   so it loads **one page and one file**, not just the first record.
3. Run it against your test site.
4. Compare with your step-1 notes: the page that was missing should now appear.

**Stretch goal:** ingest the test site into your local stack and search for a
sentence that only appears on your page.

**Done when:** the smoke test shows both a page and a file loading.

## Step 8: Clean up and open the PR

1. Run the quality gates listed in `CONTRIBUTING.md` (lint, format, typecheck,
   tests, file length, import boundaries). Python files must stay under 1,000
   lines.
2. Update `docs/users/configuration/connector-config.md` with the new setting.
3. In the PR description, include:
   - step 1 baseline results (before) and step 7 results (after)
   - the decisions you made in steps 3–5, and why
   - what's not supported yet (see "Out of scope")

## When to ask for help

Asking is expected, not a failure. Ask early if:

- You get auth or permission errors (`401`/`403`) from Graph.
- You're about to change code **outside** `connectors/sharepoint/`, the
  SharePoint projector, or `states.py`.
- Any existing test starts failing and you don't know why.
- You've been stuck on one step for more than half a day.
