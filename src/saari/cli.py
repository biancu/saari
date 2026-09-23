from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from saari import canvas as _canvas_mod, db, paths, study as _study
from saari.embed import (
    embed_papers as _embed_papers,
    query_corpus as _query_corpus,
    similar_to_paper as _similar_to_paper,
)
from saari.export import (
    export_bibtex as _export_bibtex,
    export_paper as _export_paper,
    export_prisma as _export_prisma,
    export_slides as _export_slides,
    export_slr as _export_slr,
)
from saari.models import Paper
from saari.projection import project_corpus as _project_corpus
from saari.snowball import snowball as _snowball

app = typer.Typer(
    help="saari - agent-driven literature review toolkit (saaristo project)",
    no_args_is_help=True,
)
papers_app = typer.Typer(help="Papers in the local DB", no_args_is_help=True)
searches_app = typer.Typer(help="Past searches", no_args_is_help=True)
app.add_typer(papers_app, name="papers")
app.add_typer(searches_app, name="searches")

console = Console()


class OutputFormat(str, Enum):
    table = "table"
    cards = "cards"
    md = "md"
    json = "json"


def _resolve_root() -> Path:
    try:
        return paths.project_root()
    except paths.ProjectNotFoundError as e:
        console.print(f"[red]error:[/] {e}")
        raise typer.Exit(2) from None


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Project directory (default: cwd)")] = Path.cwd(),
) -> None:
    """Initialize a saaristo project in PATH (creates .saaristo/ and papers/)."""
    existing = paths.find_project_root(path)
    if existing and existing == path.resolve():
        console.print(f"[yellow]Already a saaristo project:[/] {existing}")
        return
    root = paths.init_project(path)
    scaffolded = paths.scaffold_workspace(root)
    console.print(f"[green]Initialized saaristo project at[/] {root}")
    console.print(f"  {root / '.saaristo'}/      tool-owned state (db, raw/)")
    console.print(f"  {root / 'papers'}/        full-text PDFs and user-visible files")
    for name in scaffolded:
        console.print(f"  [cyan]{name}[/]  created/extended (harness wiring)")


@app.command()
def serve(
    port: Annotated[int, typer.Option("--port", "-p", help="HTTP port (default: 3044)")] = 3044,
    host: Annotated[str, typer.Option("--host", help="Bind address")] = "127.0.0.1",
    reload: Annotated[bool, typer.Option("--reload/--no-reload", help="Auto-reload on code changes")] = False,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open browser on start")] = True,
) -> None:
    """Boot the saari HTTP server (REST API + SPA) on PORT.

    Serves the built UI at the root if `ui/dist/` exists, otherwise just the
    API. For UI development run `cd ui && npm run dev` in a second terminal —
    Vite proxies `/api` to this server.
    """
    import threading
    import webbrowser

    try:
        # fastapi is the reliable probe: uvicorn also arrives transitively
        # via the mcp dependency, so its import succeeding proves nothing.
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError:
        console.print(
            "[red]error:[/] the HTTP server needs the `serve` extra. "
            r"Install with: uv tool install 'saari\[serve]' (or pip install 'saari\[serve]')"
        )
        raise typer.Exit(2)

    from saari.hosting import data_root

    hosted = data_root()
    if hosted is not None:
        console.print(f"[bold]saari serve[/]  hosted mode, data root: {hosted}")
    else:
        root = _resolve_root()
        console.print(f"[bold]saari serve[/]  project: {root}")
    console.print(f"  API:    http://{host}:{port}/api")
    console.print(f"  docs:   http://{host}:{port}/docs")
    console.print(f"  UI:     http://{host}:{port}/")
    if open_browser and not reload:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(
        "saari.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@app.command()
def where() -> None:
    """Print the active project root, key paths, corpus summary, and pipeline state."""
    root = _resolve_root()
    with db.connect(paths.db_path(root)) as con:
        counts = db.count_by_status(con)
        n_searches = con.execute("SELECT COUNT(*) FROM search").fetchone()[0]
        n_embedded = con.execute("SELECT COUNT(*) FROM paper_embedding_meta").fetchone()[0]
        n_projected = con.execute("SELECT COUNT(*) FROM paper_projection").fetchone()[0]
    console.print(f"[bold]project:[/] {root}")
    console.print(f"  db:      {paths.db_path(root)}")
    console.print(f"  raw:     {paths.raw_dir(root)}")
    console.print(f"  papers:  {paths.papers_dir(root)}")
    if counts:
        total = sum(counts.values())
        breakdown = "  ".join(f"{k}={v}" for k, v in counts.items())
        console.print(f"[dim]corpus:[/]   {total} papers  ({breakdown})  searches={n_searches}")
        console.print(
            f"[dim]pipeline:[/] embedded={n_embedded}/{total}  projected={n_projected}/{total}"
        )


@app.command()
def status() -> None:
    """Show where the systematic review stands: the five SLR stages.

    States are derived from the project DB (nothing is tracked by hand).
    The arrow marks the recommended next stage. Same data as the web UI
    stepper and the `review_status` MCP tool.
    """
    from saari.status import stages_data

    root = _resolve_root()
    data = stages_data(project_root=root)
    console.print(f"[bold]review status[/]  project: {root}\n")
    marks = {"done": "[green]✓[/]", "active": "[yellow]◐[/]", "todo": "[dim]○[/]"}
    for s in data["stages"]:
        arrow = "[cyan]→[/]" if s["key"] == data["next"] else " "
        console.print(f" {arrow} {marks[s['state']]} [bold]{s['label']:<9}[/] {s['detail']}")
        if s["key"] == data["next"]:
            console.print(f"      [dim]{s['hint']}[/]")
    if data["next"] is None:
        console.print("\n[green]All stages complete.[/]")


@app.command()
def refresh() -> None:
    """Run embed + project + canvas in one shot.

    Use after adding papers via search/snowball to bring the landscape up to date.
    Safe to call repeatedly (embed only touches new papers; project and canvas
    rewrite their outputs).
    """
    root = _resolve_root()
    console.print("[dim]1/3 embedding new papers...[/]")
    e = _embed_papers(only_missing=True, project_root=root)
    console.print(
        f"   [green]embedded[/] {e.n_embedded}  (skipped {e.n_skipped})  elapsed={e.elapsed_sec:.2f}s"
    )
    console.print("[dim]2/3 projecting to 2D...[/]")
    p = _project_corpus(project_root=root)
    if p.note:
        console.print(f"   [yellow]{p.note}[/]")
        return
    console.print(f"   [green]projected[/] {p.n_points} points  method={p.method}")
    console.print("[dim]3/3 rendering canvas (obsidian + html)...[/]")
    papers_dir = paths.papers_dir(root)
    oc = _canvas_mod.write_obsidian_canvas(papers_dir / "landscape.canvas", project_root=root)
    hv = _canvas_mod.write_html_viewer(papers_dir / "landscape.html", project_root=root)
    console.print(
        f"   [green]wrote[/] {oc['path']}  and  {hv['path']}  ({oc['n_nodes']} nodes)"
    )


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 25,
    year_from: Annotated[int | None, typer.Option("--year-from")] = None,
    year_to: Annotated[int | None, typer.Option("--year-to")] = None,
    source: Annotated[
        str, typer.Option("--source", "-s", help="openalex | scopus")
    ] = "openalex",
    subjarea: Annotated[
        str | None,
        typer.Option(
            "--subjarea",
            help="Scopus only: comma-separated subject-area codes to limit to, "
            "e.g. COMP,ENGI,MATH. Ignored by OpenAlex.",
        ),
    ] = None,
) -> None:
    """Search a bibliographic source and persist results into the current project.

    Scopus needs SCOPUS_API_KEY (and, off a registered institutional
    network, SCOPUS_INST_TOKEN). Results found by both sources are
    deduplicated by DOI.
    """
    from saari.sources import run_search
    from saari.sources.scopus import ScopusError

    subjareas = (
        [c.strip().upper() for c in subjarea.split(",") if c.strip()] if subjarea else None
    )

    root = _resolve_root()
    console.print(f"[dim]{source} search:[/] {query!r}  limit={limit}  @ {root}")
    try:
        result = run_search(
            query,
            source=source,
            limit=limit,
            year_from=year_from,
            year_to=year_to,
            subjareas=subjareas,
            project_root=root,
        )
    except (ValueError, ScopusError) as e:
        console.print(f"[red]error:[/] {e}")
        raise typer.Exit(2) from None
    if result["n_fetched"] == 0:
        console.print("[yellow]No results.[/]")
        return

    console.print(
        f"[green]Persisted[/] {result['n_fetched']} papers "
        f"(search #{result['search_id']}, new={result['n_new']}, "
        f"duplicate={result['n_duplicate']})"
    )
    with db.connect(paths.db_path(root)) as con:
        papers = [p for pid in result["paper_ids"] if (p := db.get_paper(con, pid))]
    _print_paper_table(papers)


@app.command()
def snowball(
    paper_id: Annotated[str, typer.Argument(help="Seed paper id (openalex:Wxxx, DOI, arxiv, or PMID)")],
    direction: Annotated[str, typer.Option("--direction", "-d", help="backward | forward | both")] = "both",
    max_per_direction: Annotated[int, typer.Option("--max", "-n", help="Max papers per direction")] = 25,
) -> None:
    """Expand a seed paper's citation neighbors into the project.

    backward: papers this one cites (via OpenAlex `referenced_works`)
    forward:  papers that cite this one (via OpenAlex `cites:` filter)
    """
    root = _resolve_root()
    result = _snowball(
        paper_id,
        direction=direction,
        max_per_direction=max_per_direction,
        project_root=root,
    )
    console.print(
        f"[green]Snowball {result.direction}[/] from {result.seed_paper_id}: "
        f"fetched={result.n_fetched}, new={result.n_new} "
        f"([cyan]backward={len(result.backward_paper_ids)}[/] / "
        f"[magenta]forward={len(result.forward_paper_ids)}[/])"
    )
    if result.skipped_reason:
        console.print(f"[yellow]note:[/] {result.skipped_reason}")


@app.command()
def embed(
    only_missing: Annotated[bool, typer.Option("--only-missing/--all", help="Skip already-embedded papers")] = True,
) -> None:
    """Embed papers in the project (local, no API cost; ~80MB model download first time).

    Uses sentence-transformers/all-MiniLM-L6-v2 (384-dim). Vectors stored in
    .saaristo/saari.db via sqlite-vec. Runs on CPU; batched; idempotent.
    """
    root = _resolve_root()
    console.print("[dim]Embedding papers with all-MiniLM-L6-v2...[/]")
    r = _embed_papers(only_missing=only_missing, project_root=root)
    console.print(
        f"[green]Embedded[/] {r.n_embedded} papers "
        f"(skipped {r.n_skipped} with no usable text)  "
        f"dim={r.dim}  model={r.model.split('/')[-1]}  elapsed={r.elapsed_sec:.2f}s"
    )


@app.command()
def query(
    text: Annotated[str, typer.Argument(help="Query text (natural language)")],
    k: Annotated[int, typer.Option("--k", "-k", help="Top-k results")] = 20,
    status: Annotated[str | None, typer.Option("--status", help="Filter by candidate|included|excluded|maybe")] = None,
    fmt: Annotated[OutputFormat, typer.Option("--format", "-f")] = OutputFormat.md,
) -> None:
    """Semantic search: rank papers by cosine similarity to the query text.

    Requires papers to be embedded (see `saari embed`). Scores are cosine
    similarity in [-1, 1]; higher is more similar.
    """
    root = _resolve_root()
    hits = _query_corpus(text, k=max(k * 3, k), project_root=root)  # over-fetch for status filtering
    if not hits:
        console.print("[yellow]No embedded papers yet. Run `saari embed` first.[/]")
        return

    score_by_id = {h.paper_id: h.score for h in hits}
    with db.connect(paths.db_path(root)) as con:
        placeholders = ",".join(["?"] * len(hits))
        sql = (
            "SELECT p.*, "
            "(SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
            f"FROM paper p WHERE p.id IN ({placeholders})"
        )
        args: list = [h.paper_id for h in hits]
        if status is not None:
            sql += " AND p.status = ?"
            args.append(status)
        rows = con.execute(sql, args).fetchall()
        papers = sorted(
            (db._row_to_paper(r) for r in rows),
            key=lambda p: score_by_id.get(p.id, 0.0),
            reverse=True,
        )[:k]

    if not papers:
        console.print("[dim]No matches after filters.[/]")
        return

    if fmt is OutputFormat.json:
        console.print_json(
            data=[
                {**_paper_card_dict(p), "score": round(score_by_id[p.id], 4)}
                for p in papers
            ]
        )
        return
    if fmt is OutputFormat.md:
        lines = [f"## Query — {len(papers)} hits for {text!r}", ""]
        for p in papers:
            meta_bits = []
            if p.year is not None:
                meta_bits.append(str(p.year))
            if p.cited_by_count is not None:
                meta_bits.append(f"cited={p.cited_by_count}")
            if p.venue:
                meta_bits.append(f"*{p.venue}*")
            meta = ", ".join(meta_bits)
            flags = _paper_flags(p)
            flag_str = f"  ·  flags: {' '.join(flags)}" if flags else ""
            lines.append(
                f"- `{p.id}` — score=**{score_by_id[p.id]:+.3f}** — **{p.title}**  ({meta}){flag_str}"
            )
            excerpt = _abstract_excerpt(p.abstract, 200)
            if excerpt:
                lines.append(f"  > {excerpt}")
            lines.append("")
        print("\n".join(lines))
        return

    # cards / table
    for p in papers:
        score = score_by_id[p.id]
        meta = [f"score={score:+.3f}"]
        if p.year is not None:
            meta.append(str(p.year))
        if p.cited_by_count is not None:
            meta.append(f"c={p.cited_by_count}")
        flags = _paper_flags(p)
        flag_str = f"  [{' '.join(flags)}]" if flags else ""
        console.print(f"[bold]{p.id}[/]  [dim]{'  '.join(meta)}[/]{flag_str}")
        console.print(f"  {p.title}")
        if p.venue:
            console.print(f"  [dim]{p.venue}[/]")
        excerpt = _abstract_excerpt(p.abstract, 200)
        if excerpt:
            console.print(f"  [dim italic]{excerpt}[/]")
        console.print()


@app.command()
def project(
    method: Annotated[str, typer.Option("--method", help="umap | pca")] = "umap",
    n_neighbors: Annotated[int, typer.Option("--n-neighbors")] = 15,
    min_dist: Annotated[float, typer.Option("--min-dist")] = 0.1,
) -> None:
    """Project embedded papers to 2D (UMAP by default; PCA fallback). Stores in paper_projection."""
    root = _resolve_root()
    console.print(f"[dim]Projecting corpus with {method}...[/]")
    r = _project_corpus(
        method=method, n_neighbors=n_neighbors, min_dist=min_dist, project_root=root
    )
    if r.note:
        console.print(f"[yellow]{r.note}[/]")
        return
    console.print(
        f"[green]Projected[/] {r.n_points} points  method={r.method}  "
        f"x=[{r.x_range[0]:.2f}, {r.x_range[1]:.2f}]  "
        f"y=[{r.y_range[0]:.2f}, {r.y_range[1]:.2f}]"
    )


@app.command()
def canvas(
    fmt: Annotated[str, typer.Option("--format", "-f", help="obsidian | html | both")] = "both",
    out: Annotated[Path | None, typer.Option("--out", help="Output path (default: papers/landscape.<ext>)")] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter by status before rendering")] = None,
) -> None:
    """Render the corpus landscape as an Obsidian .canvas file and/or a standalone HTML viewer.

    Requires `saari embed` and `saari project` first. The HTML viewer is
    self-contained (no external deps) — just open it in a browser.
    """
    root = _resolve_root()
    default_dir = paths.papers_dir(root)

    if fmt in ("obsidian", "both"):
        target = out if out and fmt == "obsidian" else default_dir / "landscape.canvas"
        r = _canvas_mod.write_obsidian_canvas(target, status=status, project_root=root)
        if r.get("note"):
            console.print(f"[yellow]{r['note']}[/]")
        else:
            console.print(
                f"[green]Wrote[/] {r['path']}  nodes={r['n_nodes']}  size={r['size_kb']} KB  "
                "[dim](open in Obsidian)[/]"
            )
    if fmt in ("html", "both"):
        target = out if out and fmt == "html" else default_dir / "landscape.html"
        r = _canvas_mod.write_html_viewer(target, status=status, project_root=root)
        if r.get("note"):
            console.print(f"[yellow]{r['note']}[/]")
        else:
            console.print(
                f"[green]Wrote[/] {r['path']}  nodes={r['n_nodes']}  size={r['size_kb']} KB  "
                "[dim](open in a browser)[/]"
            )


@app.command()
def screen(
    paper_id: Annotated[str, typer.Argument(help="Paper id, DOI, arxiv, or PMID")],
    decision: Annotated[str, typer.Argument(help="include | exclude | maybe | candidate")],
    note: Annotated[str | None, typer.Option("--note", help="Optional screening reason")] = None,
) -> None:
    """Mark a paper as included / excluded / maybe (or reset to candidate)."""
    root = _resolve_root()
    with db.connect(paths.db_path(root)) as con:
        try:
            ok = db.set_screening(con, paper_id, decision, note=note)
        except ValueError as e:
            console.print(f"[red]error:[/] {e}")
            raise typer.Exit(2) from None
    if not ok:
        console.print(f"[red]Not found:[/] {paper_id}")
        raise typer.Exit(1)
    note_s = f" ({note})" if note else ""
    console.print(f"[green]{decision}[/] {paper_id}{note_s}")


@app.command()
def triage(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    min_citations: Annotated[int | None, typer.Option("--min-citations", "-c")] = None,
    year_from: Annotated[int | None, typer.Option("--year-from")] = None,
    fmt: Annotated[OutputFormat, typer.Option("--format", "-f", help="Output format")] = OutputFormat.md,
) -> None:
    """Show the top undecided (candidate) papers as a triage checklist.

    Default output is markdown: one paper per block with `[ ]` checkbox,
    title, meta, seen_in count, and abstract excerpt. Designed to be
    scannable by agents and humans, and easy to paste into chat.
    Once you decide, run `saari screen <id> include|exclude [--note ...]`.
    """
    root = _resolve_root()
    with db.connect(paths.db_path(root)) as con:
        counts = db.count_by_status(con)
        papers = db.list_papers(
            con,
            limit=limit,
            status="candidate",
            min_citations=min_citations,
            year_from=year_from,
            order_by="COALESCE(cited_by_count, 0) DESC",
        )
    n_candidates = counts.get("candidate", 0)

    if fmt is OutputFormat.json:
        console.print_json(
            data={
                "n_candidates": n_candidates,
                "limit": limit,
                "papers": [_paper_card_dict(p) for p in papers],
            }
        )
        return

    if fmt is OutputFormat.md:
        print(_render_md_triage(papers, n_candidates, limit))
        return

    console.print(
        f"[bold]Triage[/] — showing {len(papers)} of {n_candidates} candidates"
    )
    if fmt is OutputFormat.cards:
        _print_paper_cards(papers)
    else:
        _print_paper_table(papers)


@papers_app.command("list")
def papers_list(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    offset: Annotated[int, typer.Option("--offset")] = 0,
    status: Annotated[str | None, typer.Option("--status", help="candidate|included|excluded|maybe")] = None,
    year_from: Annotated[int | None, typer.Option("--year-from")] = None,
    year_to: Annotated[int | None, typer.Option("--year-to")] = None,
    min_citations: Annotated[int | None, typer.Option("--min-citations", "-c")] = None,
    grep: Annotated[str | None, typer.Option("--grep", "-g", help="Title substring (case-insensitive)")] = None,
    seen_in_at_least: Annotated[int | None, typer.Option("--seen-in-at-least", help="Paper appeared in at least N searches")] = None,
    sort: Annotated[str, typer.Option("--sort", help="recent|cited|seen_in")] = "recent",
    fmt: Annotated[OutputFormat, typer.Option("--format", "-f")] = OutputFormat.table,
) -> None:
    """List papers in the project DB. Filters are composable; outputs are switchable."""
    order_by = {
        "recent": "first_seen_at DESC",
        "cited": "COALESCE(cited_by_count, 0) DESC",
        "seen_in": "seen_in DESC, COALESCE(cited_by_count, 0) DESC",
    }.get(sort, "first_seen_at DESC")

    root = _resolve_root()
    with db.connect(paths.db_path(root)) as con:
        papers = db.list_papers(
            con,
            limit=limit,
            offset=offset,
            status=status,
            year_from=year_from,
            year_to=year_to,
            min_citations=min_citations,
            title_grep=grep,
            seen_in_at_least=seen_in_at_least,
            order_by=order_by,
        )

    if not papers:
        console.print("[dim]No matching papers.[/]")
        return

    if fmt is OutputFormat.json:
        console.print_json(data=[_paper_card_dict(p) for p in papers])
        return
    if fmt is OutputFormat.md:
        print(_render_md_triage(papers, len(papers), len(papers), heading="Papers"))
        return
    if fmt is OutputFormat.cards:
        _print_paper_cards(papers)
        return

    _print_paper_table(papers)


@papers_app.command("similar")
def papers_similar(
    paper_id: Annotated[str, typer.Argument(help="Seed paper id, DOI, arxiv, or PMID")],
    k: Annotated[int, typer.Option("--k", "-k")] = 10,
    status: Annotated[str | None, typer.Option("--status")] = None,
    fmt: Annotated[OutputFormat, typer.Option("--format", "-f")] = OutputFormat.md,
) -> None:
    """Find papers most similar to a seed paper (cosine over embeddings).

    The seed must be embedded. Seed is excluded from results.
    """
    root = _resolve_root()
    try:
        hits = _similar_to_paper(paper_id, k=max(k * 3, k), project_root=root)
    except ValueError as e:
        console.print(f"[red]error:[/] {e}")
        raise typer.Exit(1) from None

    score_by_id = {h.paper_id: h.score for h in hits}
    with db.connect(paths.db_path(root)) as con:
        placeholders = ",".join(["?"] * len(hits)) or "NULL"
        sql = (
            "SELECT p.*, "
            "(SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
            f"FROM paper p WHERE p.id IN ({placeholders})"
        )
        args: list = [h.paper_id for h in hits]
        if status is not None:
            sql += " AND p.status = ?"
            args.append(status)
        rows = con.execute(sql, args).fetchall()
        papers = sorted(
            (db._row_to_paper(r) for r in rows),
            key=lambda p: score_by_id.get(p.id, 0.0),
            reverse=True,
        )[:k]

    if not papers:
        console.print("[dim]No similar papers (try dropping --status filter).[/]")
        return

    if fmt is OutputFormat.json:
        console.print_json(
            data=[
                {**_paper_card_dict(p), "score": round(score_by_id[p.id], 4)}
                for p in papers
            ]
        )
        return
    if fmt is OutputFormat.md:
        lines = [f"## Similar to `{paper_id}` — {len(papers)} hits", ""]
        for p in papers:
            meta_bits = []
            if p.year is not None:
                meta_bits.append(str(p.year))
            if p.cited_by_count is not None:
                meta_bits.append(f"cited={p.cited_by_count}")
            if p.venue:
                meta_bits.append(f"*{p.venue}*")
            meta = ", ".join(meta_bits)
            flags = _paper_flags(p)
            flag_str = f"  ·  flags: {' '.join(flags)}" if flags else ""
            lines.append(
                f"- `{p.id}` — score=**{score_by_id[p.id]:+.3f}** — **{p.title}**  ({meta}){flag_str}"
            )
            excerpt = _abstract_excerpt(p.abstract, 200)
            if excerpt:
                lines.append(f"  > {excerpt}")
            lines.append("")
        print("\n".join(lines))
        return
    # table / cards
    _print_paper_cards(papers)


@papers_app.command("show")
def papers_show(
    paper_id: Annotated[str, typer.Argument(help="Paper id, DOI, arxiv ID, or PMID")],
) -> None:
    """Show one paper in full detail (normalized JSON)."""
    root = _resolve_root()
    with db.connect(paths.db_path(root)) as con:
        paper = db.get_paper(con, paper_id)
    if not paper:
        console.print(f"[red]Not found:[/] {paper_id}")
        raise typer.Exit(1)
    console.print(paper.model_dump_json(indent=2))


@papers_app.command("links")
def papers_links(
    paper_id: Annotated[str, typer.Argument(help="Paper id, DOI, arxiv ID, or PMID")],
) -> None:
    """Show all known hosting locations for a paper (publisher, arXiv, PMC, repos)."""
    root = _resolve_root()
    with db.connect(paths.db_path(root)) as con:
        paper = db.get_paper(con, paper_id)
    if not paper:
        console.print(f"[red]Not found:[/] {paper_id}")
        raise typer.Exit(1)

    console.print(f"[bold]{paper.title}[/]")
    meta = []
    if paper.year:
        meta.append(str(paper.year))
    if paper.cited_by_count is not None:
        meta.append(f"cited={paper.cited_by_count}")
    if paper.oa_status:
        meta.append(f"oa={paper.oa_status}")
    if paper.doi:
        meta.append(f"doi={paper.doi}")
    if paper.arxiv_id:
        meta.append(f"arxiv={paper.arxiv_id}")
    if paper.pmid:
        meta.append(f"pmid={paper.pmid}")
    if paper.pmcid:
        meta.append(f"pmcid={paper.pmcid}")
    if meta:
        console.print("[dim]" + "  ".join(meta) + "[/]")

    if not paper.locations:
        console.print(
            "[dim](no locations recorded — pre-enrichment record; re-search to refresh)[/]"
        )
        return

    t = Table(show_header=True, header_style="bold")
    t.add_column("source")
    t.add_column("version")
    t.add_column("oa")
    t.add_column("pdf")
    t.add_column("url", overflow="fold")
    for loc in paper.locations:
        t.add_row(
            loc.source_name or "",
            loc.version or "",
            "✓" if loc.is_oa else "",
            "✓" if loc.pdf_url else "",
            loc.pdf_url or loc.landing_page_url or "",
        )
    console.print(t)


@papers_app.command("raw")
def papers_raw(
    paper_id: Annotated[str, typer.Argument(help="Paper id, DOI, arxiv ID, or PMID")],
) -> None:
    """Print the raw API response for a paper (as captured at fetch time)."""
    root = _resolve_root()
    with db.connect(paths.db_path(root)) as con:
        raw_path = db.get_raw_path(con, paper_id)
    if not raw_path:
        console.print(
            f"[red]No raw payload for:[/] {paper_id}  "
            "[dim](pre-enrichment record; re-search to capture)[/]"
        )
        raise typer.Exit(1)
    full = root / raw_path
    if not full.exists():
        console.print(f"[red]Raw file missing:[/] {full}")
        raise typer.Exit(1)
    console.print(full.read_text())


export_app = typer.Typer(help="Compile the corpus into manuscript-ready formats", no_args_is_help=True)
app.add_typer(export_app, name="export")


@export_app.command("bibtex")
def export_bibtex_cmd(
    out: Annotated[Path | None, typer.Option("--out", help="Output path (default: papers/refs.bib)")] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter by status (default: included)")] = "included",
) -> None:
    """Compile to BibTeX. One @article entry per paper, with abstract sanitized."""
    root = _resolve_root()
    target = out or (paths.papers_dir(root) / "refs.bib")
    r = _export_bibtex(target, status_filter=status, project_root=root)
    console.print(
        f"[green]Wrote[/] {r.n_entries} {status or 'all'} papers  "
        f"format={r.format}  path={r.path}"
    )


@export_app.command("prisma")
def export_prisma_cmd(
    out: Annotated[Path | None, typer.Option("--out", help="Output path (default: papers/prisma.svg)")] = None,
    fmt: Annotated[str, typer.Option("--fmt", help="svg | mermaid")] = "svg",
) -> None:
    """Render the PRISMA 2020 flow diagram (SVG or Mermaid)."""
    root = _resolve_root()
    ext = "mmd" if fmt == "mermaid" else "svg"
    target = out or (paths.papers_dir(root) / f"prisma.{ext}")
    r = _export_prisma(target, fmt=fmt, project_root=root)
    console.print(f"[green]Wrote[/] {r.format}  included={r.n_entries}  path={r.path}")


@export_app.command("paper")
def export_paper_cmd(
    out: Annotated[Path | None, typer.Option("--out", help="Output path (default: papers/paper.md)")] = None,
) -> None:
    """Assemble the Markdown SLR manuscript (writes prisma.svg, landscape.svg, refs.bib alongside)."""
    root = _resolve_root()
    target = out or (paths.papers_dir(root) / "paper.md")
    r = _export_paper(target, project_root=root)
    console.print(f"[green]Wrote[/] manuscript with {r.n_entries} included papers  path={r.path}")


@export_app.command("slides")
def export_slides_cmd(
    out: Annotated[Path | None, typer.Option("--out", help="Output path (default: papers/slides.md)")] = None,
) -> None:
    """Assemble the Marp slide deck (Markdown; render with `npx @marp-team/marp-cli`)."""
    root = _resolve_root()
    target = out or (paths.papers_dir(root) / "slides.md")
    r = _export_slides(target, project_root=root)
    console.print(f"[green]Wrote[/] deck  path={r.path}")


@export_app.command("slr")
def export_slr_cmd(
    out: Annotated[Path | None, typer.Option("--out", help="Output dir (default: papers/review/)")] = None,
) -> None:
    """One-shot: emit the whole review bundle (paper + slides + PRISMA + figures + bib)."""
    root = _resolve_root()
    r = _export_slr(out, project_root=root)
    console.print(
        f"[green]Wrote review bundle[/] ({r.n_entries} included papers) -> {r.path}\n"
        f"  paper.md · slides.md · prisma.svg · prisma.mmd · landscape.svg · refs.bib"
    )


study_app = typer.Typer(help="Study protocol: title, research question, criteria", no_args_is_help=True)
app.add_typer(study_app, name="study")


@study_app.command("show")
def study_show_cmd() -> None:
    """Print the study protocol (research question, eligibility criteria, tags)."""
    root = _resolve_root()
    console.print_json(data=_study.get(root))


@study_app.command("set")
def study_set_cmd(
    title: Annotated[str | None, typer.Option("--title")] = None,
    authors: Annotated[str | None, typer.Option("--authors")] = None,
    question: Annotated[str | None, typer.Option("--question", help="Research question")] = None,
    criteria: Annotated[str | None, typer.Option("--criteria", help="Inclusion/exclusion criteria")] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags")] = None,
) -> None:
    """Set / patch the study protocol. Only fields you pass change."""
    root = _resolve_root()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags is not None else None
    rec = _study.update(
        title=title, authors=authors, question=question,
        criteria=criteria, tags=tag_list, project_root=root,
    )
    console.print_json(data=rec)


@searches_app.command("list")
def searches_list(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
) -> None:
    """List past searches and snowball events."""
    root = _resolve_root()
    with db.connect(paths.db_path(root)) as con:
        rows = db.list_searches(con, limit=limit)
    if not rows:
        console.print("[dim]No searches yet.[/]")
        return
    t = Table(show_header=True, header_style="bold")
    t.add_column("id")
    t.add_column("source")
    t.add_column("query")
    t.add_column("n")
    t.add_column("created_at")
    for r in rows:
        t.add_row(
            str(r["id"]),
            r["source"],
            r["query"],
            str(r["n_returned"]),
            r["created_at"],
        )
    console.print(t)


# ---------- formatting helpers ----------


def _paper_flags(p: Paper) -> list[str]:
    flags = []
    if p.arxiv_id:
        flags.append("arxiv")
    if p.pmcid:
        flags.append("pmc")
    if p.oa_status and p.oa_status not in ("closed", None):
        flags.append(p.oa_status)
    if p.abstract_suspect:
        flags.append("abs?")
    if p.status != "candidate":
        flags.append(p.status)
    return flags


def _paper_card_dict(p: Paper) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "year": p.year,
        "cited_by_count": p.cited_by_count,
        "venue": p.venue,
        "doi": p.doi,
        "arxiv_id": p.arxiv_id,
        "oa_status": p.oa_status,
        "status": p.status,
        "seen_in": p.seen_in,
        "abstract_suspect": p.abstract_suspect,
        "abstract_excerpt": (p.abstract or "")[:240],
    }


def _abstract_excerpt(abstract: str | None, n: int = 280) -> str:
    if not abstract:
        return ""
    s = " ".join(abstract.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _render_md_triage(
    papers: list[Paper],
    n_candidates: int,
    limit: int,
    heading: str = "Triage",
) -> str:
    lines: list[str] = []
    lines.append(f"## {heading} — {len(papers)} of {n_candidates} shown")
    lines.append("")
    for p in papers:
        meta_bits = []
        if p.year is not None:
            meta_bits.append(str(p.year))
        if p.cited_by_count is not None:
            meta_bits.append(f"cited={p.cited_by_count}")
        if p.venue:
            meta_bits.append(f"*{p.venue}*")
        meta = ", ".join(meta_bits)
        flags = _paper_flags(p)
        flag_str = f"  ·  flags: {' '.join(flags)}" if flags else ""
        seen = f"  ·  seen_in={p.seen_in}" if p.seen_in > 1 else ""
        lines.append(f"- [ ] `{p.id}` — **{p.title}**  ({meta}){seen}{flag_str}")
        excerpt = _abstract_excerpt(p.abstract)
        if excerpt:
            lines.append(f"  > {excerpt}")
        lines.append("")
    lines.append("---")
    lines.append("Commit decisions with: `saari screen <id> include|exclude|maybe [--note ...]`")
    return "\n".join(lines)


def _print_paper_cards(papers: list[Paper]) -> None:
    for p in papers:
        meta = []
        if p.year is not None:
            meta.append(str(p.year))
        if p.cited_by_count is not None:
            meta.append(f"c={p.cited_by_count}")
        if p.seen_in > 1:
            meta.append(f"seen_in={p.seen_in}")
        flags = _paper_flags(p)
        flag_str = f"  [{' '.join(flags)}]" if flags else ""
        console.print(f"[bold]{p.id}[/]  [dim]{'  '.join(meta)}[/]{flag_str}")
        console.print(f"  {p.title}")
        if p.venue:
            console.print(f"  [dim]{p.venue}[/]")
        excerpt = _abstract_excerpt(p.abstract, 200)
        if excerpt:
            console.print(f"  [dim italic]{excerpt}[/]")
        console.print()


def _print_paper_table(papers: list[Paper]) -> None:
    t = Table(show_header=True, header_style="bold")
    t.add_column("id", overflow="fold", max_width=22)
    t.add_column("year")
    t.add_column("cited")
    t.add_column("seen")
    t.add_column("flags")
    t.add_column("title", overflow="fold")
    t.add_column("venue", overflow="fold", max_width=28)
    for p in papers:
        flags = _paper_flags(p)
        t.add_row(
            p.id,
            str(p.year or ""),
            str(p.cited_by_count or ""),
            str(p.seen_in) if p.seen_in > 1 else "",
            " ".join(flags),
            p.title,
            p.venue or "",
        )
    console.print(t)


if __name__ == "__main__":
    app()
