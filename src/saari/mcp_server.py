"""MCP server exposing saari skills to agents (Claude Code, custom agents, etc.).

Run via `saari-mcp` (stdio). The host (Claude Code) typically starts the server with
a project directory as cwd; saari then resolves the project root by walking up for
`.saaristo/`, or honors `SAARI_PROJECT_ROOT`.

Every mutating tool persists to the project DB and writes raw API payloads to
`.saaristo/raw/<source>/<id>.json` — the agent can `Read` those directly when it
wants ground-truth JSON instead of normalized fields.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from pathlib import Path

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

mcp = FastMCP("saari")


class PaperCard(BaseModel):
    """Compact paper summary — returned from list / search / triage tools so
    multi-paper responses stay small. Call `paper_show` for the full record."""

    id: str
    title: str
    year: int | None = None
    cited_by_count: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    oa_status: str | None = None
    status: str = "candidate"
    seen_in: int = 0
    abstract_suspect: bool = False
    abstract_excerpt: str | None = None


def _to_card(p: Paper, include_excerpt: bool = False) -> PaperCard:
    excerpt: str | None = None
    if include_excerpt and p.abstract:
        s = " ".join(p.abstract.split())
        excerpt = s if len(s) <= 240 else s[:239].rstrip() + "…"
    return PaperCard(
        id=p.id,
        title=p.title,
        year=p.year,
        cited_by_count=p.cited_by_count,
        venue=p.venue,
        doi=p.doi,
        arxiv_id=p.arxiv_id,
        oa_status=p.oa_status,
        status=p.status,
        seen_in=p.seen_in,
        abstract_suspect=p.abstract_suspect,
        abstract_excerpt=excerpt,
    )


@mcp.tool()
def project_info() -> dict[str, Any]:
    """Report the active saaristo project root, key paths, corpus counts, and pipeline state.

    `pipeline` tells the agent which operations are ready to run:
    - `embedded`: how many papers have vectors (needed for query/similar/project/canvas)
    - `projected`: how many have 2D coords (needed for canvas)
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        n_papers = con.execute("SELECT COUNT(*) FROM paper").fetchone()[0]
        n_searches = con.execute("SELECT COUNT(*) FROM search").fetchone()[0]
        by_status = db.count_by_status(con)
        n_embedded = con.execute("SELECT COUNT(*) FROM paper_embedding_meta").fetchone()[0]
        n_projected = con.execute("SELECT COUNT(*) FROM paper_projection").fetchone()[0]
    return {
        "root": str(root),
        "db": str(paths.db_path(root)),
        "raw_dir": str(paths.raw_dir(root)),
        "papers_dir": str(paths.papers_dir(root)),
        "n_papers": n_papers,
        "n_searches": n_searches,
        "by_status": by_status,
        "pipeline": {
            "embedded": n_embedded,
            "projected": n_projected,
        },
    }


@mcp.tool()
def review_status() -> dict[str, Any]:
    """Where the systematic review stands: the five SLR stages, derived from the DB.

    Stages: protocol -> search -> screen -> snowball -> report. Each has
    `state` (done | active | todo), a factual `detail`, and a methodology
    `hint`. `next` names the first stage that is not done -- the recommended
    place to work. Use this to answer "where are we?" and to pick the next
    action; the same data drives the stepper in the web UI.
    """
    from saari.status import stages_data

    return stages_data(project_root=paths.project_root())


@mcp.tool()
def search(
    query: str,
    limit: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    source: str = "openalex",
) -> dict[str, Any]:
    """Search a bibliographic source and persist results into the project.

    `source`: "openalex" (default, keyless) or "scopus" (needs
    SCOPUS_API_KEY, and SCOPUS_INST_TOKEN off a registered institutional
    network; Scopus records may lack abstracts). Results are deduplicated
    across sources by DOI.

    Returns a summary (`search_id`, counts, `paper_ids`). Use `papers_list`
    or `paper_show` to read the actual paper data.
    """
    from saari.sources import run_search

    return run_search(
        query,
        source=source,
        limit=limit,
        year_from=year_from,
        year_to=year_to,
        project_root=paths.project_root(),
    )


@mcp.tool()
def papers_list(
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    min_citations: int | None = None,
    title_grep: str | None = None,
    seen_in_at_least: int | None = None,
    sort: str = "recent",
    include_excerpt: bool = False,
) -> dict[str, Any]:
    """List papers in the project DB with composable filters. Returns compact PaperCards.

    Filters:
    - `status`: candidate | included | excluded | maybe
    - `year_from` / `year_to`: inclusive
    - `min_citations`: minimum cited_by_count
    - `title_grep`: case-insensitive title substring
    - `seen_in_at_least`: paper appeared in at least N distinct searches (useful for relevance)

    `sort`: recent (first_seen_at desc, default) | cited (citations desc) | seen_in (overlap desc).
    `include_excerpt`: include up to 240 chars of abstract in each card. Call `paper_show` for full.
    """
    root = paths.project_root()
    order_by = {
        "recent": "first_seen_at DESC",
        "cited": "COALESCE(cited_by_count, 0) DESC",
        "seen_in": "seen_in DESC, COALESCE(cited_by_count, 0) DESC",
    }.get(sort, "first_seen_at DESC")

    with db.connect(paths.db_path(root)) as con:
        papers = db.list_papers(
            con,
            limit=limit,
            offset=offset,
            status=status,
            year_from=year_from,
            year_to=year_to,
            min_citations=min_citations,
            title_grep=title_grep,
            seen_in_at_least=seen_in_at_least,
            order_by=order_by,
        )
    return {
        "n": len(papers),
        "papers": [_to_card(p, include_excerpt=include_excerpt).model_dump() for p in papers],
    }


@mcp.tool()
def triage(
    limit: int = 20,
    min_citations: int | None = None,
    year_from: int | None = None,
    seen_in_at_least: int | None = None,
) -> dict[str, Any]:
    """Return the top undecided (status='candidate') papers as compact cards with
    abstract excerpts — sorted by citation count. The triage workflow: call this,
    scan the cards, then call `screen` for each decision.

    Returns `{n_candidates, papers}`. `n_candidates` is the full pool (not the
    `limit`-slice), so the agent knows how much work remains.
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        counts = db.count_by_status(con)
        papers = db.list_papers(
            con,
            limit=limit,
            status="candidate",
            min_citations=min_citations,
            year_from=year_from,
            seen_in_at_least=seen_in_at_least,
            order_by="COALESCE(cited_by_count, 0) DESC",
        )
    return {
        "n_candidates": counts.get("candidate", 0),
        "shown": len(papers),
        "papers": [_to_card(p, include_excerpt=True).model_dump() for p in papers],
    }


@mcp.tool()
def screen(
    paper_id: str,
    decision: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Mark a paper as included / excluded / maybe (or reset to candidate).

    Accepts aliases: include→included, exclude/reject→excluded, keep/yes→included,
    no→excluded, reset→candidate.

    Returns `{paper_id, status, ok}`. Status is the normalized stored value.
    """
    root = paths.project_root()
    normalized = db.normalize_status(decision)
    with db.connect(paths.db_path(root)) as con:
        ok = db.set_screening(con, paper_id, normalized, note=note)
    return {"paper_id": paper_id, "status": normalized, "ok": ok}


@mcp.tool()
def paper_show(paper_id: str) -> dict[str, Any] | None:
    """Return the full Paper record (authors, abstract, references, locations, etc.).

    Accepts an OpenAlex id (`openalex:Wxxx` or bare `Wxxx`), DOI, arxiv ID, or PMID.
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        p = db.get_paper(con, paper_id)
    return p.model_dump(mode="json") if p else None


@mcp.tool()
def paper_links(paper_id: str) -> dict[str, Any] | None:
    """Return all known hosting locations — publisher page, arXiv, PMC, repositories.

    Best source for "where can I get the PDF?" — look for `locations[].pdf_url`
    where `is_oa` is True.
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        p = db.get_paper(con, paper_id)
    if not p:
        return None
    return {
        "id": p.id,
        "title": p.title,
        "doi": p.doi,
        "arxiv_id": p.arxiv_id,
        "pmid": p.pmid,
        "pmcid": p.pmcid,
        "oa_status": p.oa_status,
        "landing_page_url": p.landing_page_url,
        "pdf_url": p.pdf_url,
        "locations": [loc.model_dump() for loc in p.locations],
    }


@mcp.tool()
def paper_raw(paper_id: str) -> dict[str, Any] | None:
    """Return the raw source API response (OpenAlex JSON) captured at fetch time.

    Useful when the normalized fields miss nuance, when OpenAlex mis-merged a
    record, or when you want to see fields we don't yet extract.
    Response includes both the filesystem `path` (you can `Read` it) and the `content`.
    """
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        raw_path = db.get_raw_path(con, paper_id)
    if not raw_path:
        return None
    full = root / raw_path
    if not full.exists():
        return None
    return {"path": str(full), "content": full.read_text()}


@mcp.tool()
def searches_list(limit: int = 20) -> dict[str, Any]:
    """List past searches and snowball events in this project."""
    root = paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        rows = db.list_searches(con, limit=limit)
    return {"searches": rows}


@mcp.tool()
def snowball(
    paper_id: str,
    direction: str = "both",
    max_per_direction: int = 25,
) -> dict[str, Any]:
    """Expand a seed paper's citation neighbors into the project DB.

    - `backward`: papers the seed cites (from OpenAlex `referenced_works`)
    - `forward`:  papers that cite the seed (via OpenAlex `cites:` filter)
    - `both`:     both, up to `max_per_direction` each

    Often the single highest-leverage move after finding one good seed —
    e.g. a survey paper gets you its whole bibliography in one call.
    """
    root = paths.project_root()
    r = _snowball(
        paper_id,
        direction=direction,
        max_per_direction=max_per_direction,
        project_root=root,
    )
    return {
        "seed": r.seed_paper_id,
        "direction": r.direction,
        "n_fetched": r.n_fetched,
        "n_new": r.n_new,
        "backward_paper_ids": r.backward_paper_ids,
        "forward_paper_ids": r.forward_paper_ids,
        "skipped_reason": r.skipped_reason,
    }


@mcp.tool()
def embed(only_missing: bool = True) -> dict[str, Any]:
    """Embed papers in the project using sentence-transformers/all-MiniLM-L6-v2 (384-dim).

    Local compute — no API cost. First invocation downloads ~80MB model.
    Vectors persist in .saaristo/saari.db via sqlite-vec. Safe to call repeatedly
    with only_missing=True (default).

    Returns `{n_embedded, n_skipped, model, dim, elapsed_sec}`.
    """
    r = _embed_papers(only_missing=only_missing, project_root=paths.project_root())
    return {
        "n_embedded": r.n_embedded,
        "n_skipped": r.n_skipped,
        "model": r.model,
        "dim": r.dim,
        "elapsed_sec": round(r.elapsed_sec, 3),
    }


@mcp.tool()
def query(
    text: str,
    k: int = 20,
    status: str | None = None,
    include_excerpt: bool = True,
) -> dict[str, Any]:
    """Semantic search: rank papers by cosine similarity to the query text.

    Requires `embed` to have been run. Scores are cosine similarity in [-1, 1];
    higher is more similar. Post-filtering by status is applied after vector
    retrieval — over-fetches 3x to compensate.

    Returns `{query, n, papers}` where each paper card includes a `score` field.
    """
    root = paths.project_root()
    hits = _query_corpus(text, k=max(k * 3, k), project_root=root)
    if not hits:
        return {"query": text, "n": 0, "papers": [], "note": "No embedded papers. Run `embed` first."}

    score_by_id = {h.paper_id: h.score for h in hits}
    with db.connect(paths.db_path(root)) as con:
        placeholders = ",".join(["?"] * len(hits))
        sql = (
            "SELECT p.*, "
            "(SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
            f"FROM paper p WHERE p.id IN ({placeholders})"
        )
        args: list[Any] = [h.paper_id for h in hits]
        if status is not None:
            sql += " AND p.status = ?"
            args.append(status)
        rows = con.execute(sql, args).fetchall()
        papers = sorted(
            (db._row_to_paper(r) for r in rows),
            key=lambda p: score_by_id.get(p.id, 0.0),
            reverse=True,
        )[:k]

    return {
        "query": text,
        "n": len(papers),
        "papers": [
            {
                **_to_card(p, include_excerpt=include_excerpt).model_dump(),
                "score": round(score_by_id[p.id], 4),
            }
            for p in papers
        ],
    }


@mcp.tool()
def papers_similar(
    paper_id: str,
    k: int = 10,
    status: str | None = None,
    include_excerpt: bool = True,
) -> dict[str, Any]:
    """Find papers most similar to a seed paper (cosine over embeddings).

    The seed must already be embedded. Returns ranked PaperCards, seed excluded.
    Great after triaging a strong include — "find more like this".
    """
    root = paths.project_root()
    try:
        hits = _similar_to_paper(paper_id, k=max(k * 3, k), project_root=root)
    except ValueError as e:
        return {"error": str(e), "papers": []}

    score_by_id = {h.paper_id: h.score for h in hits}
    with db.connect(paths.db_path(root)) as con:
        placeholders = ",".join(["?"] * len(hits)) or "NULL"
        sql = (
            "SELECT p.*, "
            "(SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
            f"FROM paper p WHERE p.id IN ({placeholders})"
        )
        args: list[Any] = [h.paper_id for h in hits]
        if status is not None:
            sql += " AND p.status = ?"
            args.append(status)
        rows = con.execute(sql, args).fetchall()
        papers = sorted(
            (db._row_to_paper(r) for r in rows),
            key=lambda p: score_by_id.get(p.id, 0.0),
            reverse=True,
        )[:k]

    return {
        "seed": paper_id,
        "n": len(papers),
        "papers": [
            {
                **_to_card(p, include_excerpt=include_excerpt).model_dump(),
                "score": round(score_by_id[p.id], 4),
            }
            for p in papers
        ],
    }


@mcp.tool()
def project(
    method: str = "umap",
    n_neighbors: int = 15,
    min_dist: float = 0.1,
) -> dict[str, Any]:
    """Project embedded papers to 2D (UMAP by default; numpy-SVD PCA fallback).

    Persists to paper_projection. Prerequisite: `embed`.
    Returns `{n_points, method, params, x_range, y_range}`.
    """
    r = _project_corpus(
        method=method,
        n_neighbors=n_neighbors,
        min_dist=min_dist,
        project_root=paths.project_root(),
    )
    return {
        "n_points": r.n_points,
        "method": r.method,
        "params": r.params,
        "x_range": r.x_range,
        "y_range": r.y_range,
        "note": r.note,
    }


@mcp.tool()
def canvas(
    format: str = "both",
    status: str | None = None,
    out: str | None = None,
) -> dict[str, Any]:
    """Render the corpus landscape as an Obsidian .canvas file and/or a standalone HTML viewer.

    `format`: obsidian | html | both.
    Prerequisite: `embed` + `project`. Artifacts default to `papers/landscape.*`.
    HTML is self-contained (no external deps); open directly in a browser.
    """
    root = paths.project_root()
    default_dir = paths.papers_dir(root)
    results: dict[str, Any] = {}

    if format in ("obsidian", "both"):
        target = Path(out) if out and format == "obsidian" else default_dir / "landscape.canvas"
        results["obsidian"] = _canvas_mod.write_obsidian_canvas(
            target, status=status, project_root=root
        )
    if format in ("html", "both"):
        target = Path(out) if out and format == "html" else default_dir / "landscape.html"
        results["html"] = _canvas_mod.write_html_viewer(
            target, status=status, project_root=root
        )
    return results


@mcp.tool()
def export_bibtex(
    status: str | None = "included",
    out: str | None = None,
) -> dict[str, Any]:
    """Export papers to a BibTeX file for the manuscript.

    Defaults to status='included' and writes to `papers/refs.bib`.
    Citation keys follow `<firstauthorlastname><year><firsttitleword>`.
    Abstracts longer than 1500 chars or containing publisher front-matter
    noise markers are dropped from the entry (preserves a clean .bib file).
    """
    root = paths.project_root()
    target = Path(out) if out else paths.papers_dir(root) / "refs.bib"
    r = _export_bibtex(target, status_filter=status, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def export_prisma(fmt: str = "svg", out: str | None = None) -> dict[str, Any]:
    """Render the PRISMA 2020 flow diagram from the screening funnel.

    `fmt`: 'svg' (default, self-contained) | 'mermaid'. Defaults to
    `papers/prisma.svg`. Counts trace to the DB via `funnel()`.
    """
    root = paths.project_root()
    ext = "mmd" if fmt == "mermaid" else "svg"
    target = Path(out) if out else paths.papers_dir(root) / f"prisma.{ext}"
    r = _export_prisma(target, fmt=fmt, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def export_paper(out: str | None = None) -> dict[str, Any]:
    """Assemble the Markdown SLR manuscript scaffold (default `papers/paper.md`).

    Mechanical content (PRISMA counts, search-strategy table, study-characteristics
    table, references, figures) is auto-filled and traceable; synthesis is left as
    `<!-- WRITE -->` slots for a human/LLM. Also writes prisma.svg, landscape.svg,
    refs.bib alongside. Set the protocol first with `study_set`.
    """
    root = paths.project_root()
    target = Path(out) if out else paths.papers_dir(root) / "paper.md"
    r = _export_paper(target, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def export_slides(out: str | None = None) -> dict[str, Any]:
    """Assemble the Marp slide deck (default `papers/slides.md`)."""
    root = paths.project_root()
    target = Path(out) if out else paths.papers_dir(root) / "slides.md"
    r = _export_slides(target, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def export_slr(out_dir: str | None = None) -> dict[str, Any]:
    """One-shot: emit the full review bundle (paper + slides + PRISMA + figures + bib).

    The headless entry point — turns a screened corpus into
    `papers/review/{paper.md, slides.md, prisma.svg, prisma.mmd, landscape.svg,
    refs.bib}`. Set the protocol (`study_set`) and screen papers first.
    """
    root = paths.project_root()
    r = _export_slr(Path(out_dir) if out_dir else None, project_root=root)
    return {"path": r.path, "n_entries": r.n_entries, "format": r.format}


@mcp.tool()
def refresh() -> dict[str, Any]:
    """One-shot: embed new papers, re-project, regenerate canvas artifacts.

    Use after `search` / `snowball` to bring the corpus landscape up to date.
    Returns per-stage counts + artifact paths.
    """
    root = paths.project_root()
    e = _embed_papers(only_missing=True, project_root=root)
    p = _project_corpus(project_root=root)
    if p.note:
        return {"embed": e.__dict__, "project": {"note": p.note}, "canvas": None}
    papers_dir = paths.papers_dir(root)
    oc = _canvas_mod.write_obsidian_canvas(
        papers_dir / "landscape.canvas", project_root=root
    )
    hv = _canvas_mod.write_html_viewer(
        papers_dir / "landscape.html", project_root=root
    )
    return {
        "embed": {
            "n_embedded": e.n_embedded,
            "n_skipped": e.n_skipped,
            "elapsed_sec": round(e.elapsed_sec, 3),
        },
        "project": {
            "n_points": p.n_points,
            "method": p.method,
            "x_range": p.x_range,
            "y_range": p.y_range,
        },
        "canvas": {"obsidian": oc, "html": hv},
    }


@mcp.tool()
def study_get() -> dict[str, Any]:
    """Read the study metadata: research question, inclusion/exclusion criteria, tags.

    Stored at `.saaristo/study.json` and editable via `study_set`. Returns the full
    shape with empty defaults so callers can render without null-checks.
    """
    return _study.get(project_root=paths.project_root())


@mcp.tool()
def study_set(
    title: str | None = None,
    authors: str | None = None,
    question: str | None = None,
    criteria: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Set / patch study metadata. Only fields you pass change.

    - `title`: manuscript title (used by `export_paper`).
    - `authors`: author line (free text).
    - `question`: the research question (markdown OK).
    - `criteria`: inclusion/exclusion criteria (markdown OK).
    - `tags`: free-form tags. Pass `[]` to clear, `None` to leave alone.

    Returns the full updated study record.
    """
    return _study.update(
        title=title,
        authors=authors,
        question=question,
        criteria=criteria,
        tags=tags,
        project_root=paths.project_root(),
    )


@mcp.tool()
def funnel() -> dict[str, Any]:
    """Return PRISMA-style aggregate: searches, fetched, unique, screened.

    Use to summarize review progress in narrative or to render a flow diagram.
    Includes per-search rows and per-status counts.
    """
    return _study.funnel(project_root=paths.project_root())


def main() -> None:
    """Run the MCP server.

    Default: stdio, for local harnesses (Claude Code, Claude Desktop) that
    spawn the process per project directory.

    `saari-mcp --http` serves the same tools over streamable HTTP for remote
    hosts (Copilot Studio, hosted deployments). With SAARI_DATA_ROOT set,
    each request is scoped to the caller's project directory via the shared
    hosting middleware; without it, the server serves the project it was
    started in, like stdio mode.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="saari-mcp")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3045)
    args = parser.parse_args()

    if not args.http:
        mcp.run()
        return

    import uvicorn

    from saari.hosting import RequestRootMiddleware

    # Stateless: no server-side session pinning, so replicas stay
    # interchangeable and simple HTTP clients work.
    mcp.settings.stateless_http = True
    app = RequestRootMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
