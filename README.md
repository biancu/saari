# saari

Agent-driven literature review toolkit. Part of the *saaristo* project at
Novia University of Applied Sciences.

saari is not an app with a "do literature review" button. It is a set of
small, composable operations — search, snowball, screen, embed, map, export —
that an agent (Claude Code, any MCP host, or you at a shell) calls in
whatever order the work requires. All state lives in a `.saaristo/` directory
inside your own project folder, in SQLite. Local-first, no accounts, and the
core loop needs no API keys: search uses OpenAlex, embeddings run locally.

**Sources.** OpenAlex is the default (keyless). Scopus is available as a
second database (`saari search "..." --source scopus`): set `SCOPUS_API_KEY`
(create one at dev.elsevier.com) and note that a key alone only works from a
network Elsevier has registered for your institution's subscription — off
that network you also need `SCOPUS_INST_TOKEN`, an institutional token your
library requests from Elsevier. Results are deduplicated across sources by
DOI, PRISMA reporting splits identification per database, and Scopus search
records usually arrive without abstracts (Elsevier entitlement) — a matching
OpenAlex hit on the same DOI supplies the abstract.

One toolkit, three surfaces with the same operations:

- **MCP server** (`saari-mcp`) — for agents. The primary interface.
- **CLI** (`saari`) — for humans at a shell and for scripts.
- **HTTP server + web UI** (`saari serve`) — corpus browser, screening view,
  interactive UMAP landscape. Optional.

## Quickstart (with an agent harness)

The intended setup: you are in a folder where you are writing a paper, and
your agent harness (e.g. Claude Code) works there with you.

```bash
uv tool install saari        # or: pipx install saari
cd my-paper/
saari init
```

Register the MCP server in the folder's `.mcp.json`:

```json
{ "mcpServers": { "saari": { "command": "uvx", "args": ["saari-mcp"] } } }
```

Optionally install the literature-review skill, which teaches the agent the
systematic-review methodology (protocol first, screen with reasons, PRISMA
at the end):

```bash
npx skills add Novia-RDI-Seafaring/saari --skill litreview
```

Then ask your agent to start a literature review. It will set the protocol,
search, snowball, screen, and export — and the outputs land as plain files
in your folder, next to your draft.

## Quickstart (CLI only)

```bash
cd my-paper/
saari init
saari study set --title "..." --question "..." --criteria "..."
saari search "digital twin maritime safety" --limit 50
saari snowball openalex:W1234567890
saari refresh                      # embed new papers + rebuild the landscape
saari triage                       # screen candidates
saari export slr                   # PRISMA + manuscript scaffold + slides
```

`saari --help` lists everything. For the web UI, install the serve extra
(`uv tool install 'saari[serve]'`) and run `saari serve`.

## What the export gives you

`saari export slr` writes `papers/review/`:

- a **PRISMA 2020 flow diagram** computed from your actual search and
  screening funnel,
- a **Markdown SLR manuscript scaffold** — counts, tables, references and
  figures auto-filled and traceable to the database; interpretive prose
  left as explicit `<!-- WRITE: ... -->` slots,
- a slide deck (Marp).

The governing rule is **never fabricate**: saari fills in only what it can
trace to the corpus, and always writes an honest Limitations section.

## Layout on disk

```
my-paper/
├── .saaristo/       tool-owned state (SQLite DB, raw API responses)
└── papers/          user-visible outputs (landscape.html, refs.bib, review/)
```

Delete `.saaristo/` and the project is gone; zip the folder and the whole
review travels with it.

## Hosted mode (experimental)

saari is local-first, but every surface can also run multi-user behind an
authenticating proxy (e.g. Azure Container Apps with Entra ID "Easy Auth"):

```bash
SAARI_DATA_ROOT=/data saari serve          # HTTP API + UI, per-user projects
SAARI_DATA_ROOT=/data saari-mcp --http     # MCP over streamable HTTP (for
                                           # Copilot Studio and remote hosts)
```

Each request is scoped to `$SAARI_DATA_ROOT/<user>/<project>/` from the
`x-ms-client-principal-id` (or `x-saari-user`) and `x-saari-project`
headers. The auth layer in front must inject these; saari trusts them.
Requests without an identity get 401. Without `SAARI_DATA_ROOT`, nothing
changes: single project, resolved from the working directory.

A `Dockerfile` ships in the repo (SPA build + `saari[serve]`, default CMD
`saari serve`); `docs/hosting.md` walks through an Azure Container Apps +
Entra ID deployment.

## Development

```bash
git clone https://github.com/Novia-RDI-Seafaring/saari
cd saari
uv sync --all-extras
uv run pytest -q
```

The web UI is a React SPA in `ui/` (pnpm + Vite): `cd ui && pnpm install &&
pnpm dev` proxies `/api` to a running `saari serve`.

## License

Apache-2.0. See `LICENSE`.
