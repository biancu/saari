---
name: litreview
description: >
  Conduct a systematic literature review with the saari toolkit: set a review
  protocol, search OpenAlex, snowball references, screen with recorded reasons,
  map the corpus landscape, and export a PRISMA-traced SLR draft. Use when the
  user wants to do a literature review, find prior work, screen papers, build a
  reference list, or generate a PRISMA diagram or review manuscript scaffold.
---

# Systematic literature review with saari

saari is a local-first literature-review toolkit. All state lives in a
`.saaristo/` directory inside the current project folder (usually the folder
where the user is writing their paper). It exposes the same operations as a
CLI (`saari`), an MCP server (`saari-mcp`), and an HTTP server with a web UI
(`saari serve`). Prefer the MCP tools when they are available; fall back to
the CLI otherwise.

## Step 0: bootstrap

1. Check whether the saari MCP tools (`search`, `screen`, `papers_list`, ...)
   are available in this session. If yes, skip to Step 1.
2. Check the CLI: `uvx saari where`. If saari is not installed, install it
   with `uv tool install saari` (or use `uvx saari ...` per call).
3. If the folder is not yet a saari project, run `saari init`. This creates
   `.saaristo/` (tool state) and `papers/` (user-visible outputs).
4. Offer to register the MCP server for future sessions by merging this into
   the project's `.mcp.json` (never overwrite existing entries):

   ```json
   { "mcpServers": { "saari": { "command": "uvx", "args": ["saari-mcp"] } } }
   ```

## Step 1: protocol before searching

Never search before the protocol is set. Ask the user for, then record:

- the research question,
- inclusion and exclusion criteria,
- a working title.

Record them with `saari study set --title ... --question ... --criteria ...`
(CLI) or the `study` MCP tool. Screening decisions made without recorded
criteria are not systematic and will weaken the exported review.

## Step 2: search wide

- Run several differently-phrased searches (`search`), not one. OpenAlex is
  keyword-sensitive; 3-6 query variants with year bounds beat one big query.
- If `SCOPUS_API_KEY` is configured, repeat the key queries with
  `source="scopus"` — a second database strengthens PRISMA coverage, and
  results are deduplicated by DOI automatically. If Scopus errors mention
  IP ranges or InstToken, relay the message to the user (it is an
  institutional-access problem, not a bug) and continue with OpenAlex.
- Duplicates are deduplicated automatically; a paper seen in multiple
  searches is a relevance signal (`seen_in` on paper cards).
- After searching, run `refresh` once to embed new papers and re-project the
  landscape. Then use `query` (semantic search over the corpus) to check
  coverage: if an obviously relevant phrasing returns thin results, search
  again with new terms.

## Step 3: snowball

For each clearly relevant paper found, run `snowball` (references and
citations via OpenAlex) to pull in its neighborhood. Repeat on newly found
key papers until snowballing stops surfacing new relevant work (saturation).
Run `refresh` after snowball rounds.

## Step 4: screen with reasons

- Screen every candidate to `included`, `excluded`, or `maybe` using
  `screen`, and always record a short note with the reason, phrased against
  the protocol's criteria.
- Use `triage` / `papers_list` to work through candidates in batches; read
  abstracts (`paper_show`) before deciding. Do not screen on titles alone
  unless the title alone establishes an exclusion criterion.
- Revisit `maybe` papers at the end; a review should ship with zero maybes.
- The user is the reviewer of record: for borderline calls, present the
  abstract and your reasoning and let the user decide.

## Step 5: map the landscape

- `saari serve` starts the local web UI (corpus, screening, graph views) if
  the user wants to look at the corpus visually; it is optional.
- `canvas` regenerates `papers/landscape.html` and an Obsidian canvas —
  a UMAP map of the corpus useful for spotting clusters and gaps.
- `papers_similar` finds nearest neighbors of a given paper.

## Step 6: export

- `export bibtex` writes `papers/refs.bib` for citing from the draft.
- `export slr` writes the full bundle to `papers/review/`: a PRISMA 2020
  flow diagram from the actual search/screen funnel, a Markdown SLR
  manuscript scaffold, and a slide deck.
- Everything mechanical in the export (counts, tables, references, PRISMA
  numbers) traces to the database. Interpretive prose is emitted as
  `<!-- WRITE: ... -->` slots.

## Hard rules

- **Never fabricate.** Do not invent papers, findings, counts, or citations.
  Only fill a `WRITE:` slot with claims you can trace to included papers,
  and cite them.
- Do not mark a paper `included`/`excluded` without a recorded reason.
- Do not delete or hand-edit files under `.saaristo/` — it is tool-owned.
- Do not weaken the exported Limitations section; it exists so drafts
  cannot overclaim.

## Verification

The review is in a sound state when: the protocol fields are set; zero
candidates and zero maybes remain; every included/excluded paper has a note;
`refresh` reports all papers embedded and projected; and `export slr` runs
without warnings and its PRISMA counts match `project_info`.
