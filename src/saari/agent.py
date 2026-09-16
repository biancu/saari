"""Pydantic AI agent — drives the saari core for fuzzy intent.

Direct API calls handle precise intent ("include this paper", "show this UMAP").
The agent is for fuzzy intent ("snowball outward from these three until you
hit 50 candidates", "summarize this cluster"). Same DB, same primitives —
the agent is just another caller of the saari functions.

This agent's tool surface mirrors the MCP server's tools so the in-app chat,
Claude Code, and the REST API all expose the same operations. If you add a
capability, add it here too.

If no `OPENAI_API_KEY` is set, the agent stream emits a single message
explaining how to enable it. The chat panel still works as a UI surface.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from saari import canvas as _canvas_mod, db, paths, study as _study
from saari.embed import (
    embed_papers as _embed_papers,
    query_corpus as _query_corpus,
    similar_to_paper as _similar_to_paper,
)
from saari.export import export_bibtex as _export_bibtex
from saari.projection import project_corpus as _project_corpus
from saari.snowball import snowball as _snowball


def _has_api_key() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


def _build_agent():  # type: ignore[no-untyped-def]
    """Lazy-build the Pydantic AI agent so missing keys don't break imports."""
    from pydantic_ai import Agent

    model_id = os.environ.get("SAARI_AGENT_MODEL", "openai:gpt-4o-mini")
    agent = Agent(
        model_id,
        system_prompt=(
            "You are a literature-review assistant embedded in the saari UI. "
            "You drive a SQLite-backed paper corpus through tool calls. "
            "Be terse. Prefer tool calls over speculation. When you finish a task, "
            "report what changed in 1-2 sentences. If a tool errors, summarize the error "
            "and suggest the most likely fix instead of retrying blindly."
        ),
    )

    # ---------- study + project context ----------

    @agent.tool_plain
    def project_info() -> dict[str, Any]:
        """Active project root, key paths, corpus counts, pipeline state."""
        root = paths.project_root()
        with db.connect(paths.db_path(root)) as con:
            n_papers = con.execute("SELECT COUNT(*) FROM paper").fetchone()[0]
            n_searches = con.execute("SELECT COUNT(*) FROM search").fetchone()[0]
            by_status = db.count_by_status(con)
            n_embedded = con.execute("SELECT COUNT(*) FROM paper_embedding_meta").fetchone()[0]
            n_projected = con.execute("SELECT COUNT(*) FROM paper_projection").fetchone()[0]
        return {
            "root": str(root), "n_papers": n_papers, "n_searches": n_searches,
            "by_status": by_status,
            "pipeline": {"embedded": n_embedded, "projected": n_projected},
        }

    @agent.tool_plain
    def study_get() -> dict[str, Any]:
        """Read the study metadata (research question, criteria, tags)."""
        return _study.get()

    @agent.tool_plain
    def study_set(
        question: str | None = None,
        criteria: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Patch study metadata. Only fields you pass change."""
        return _study.update(question=question, criteria=criteria, tags=tags)

    @agent.tool_plain
    def funnel() -> dict[str, Any]:
        """PRISMA-style aggregate: searches, fetched, unique, screened, by_status."""
        return _study.funnel()

    # ---------- searches ----------

    @agent.tool_plain
    def search(
        query: str,
        limit: int = 25,
        year_from: int | None = None,
        year_to: int | None = None,
        source: str = "openalex",
    ) -> dict[str, Any]:
        """Search a bibliographic source ("openalex" or "scopus") and persist results.

        Returns search_id + counts. Scopus needs SCOPUS_API_KEY configured.
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

    @agent.tool_plain
    def searches_list(limit: int = 20) -> dict[str, Any]:
        """List past searches in this project."""
        with db.connect(paths.db_path()) as con:
            rows = db.list_searches(con, limit=limit)
        return {"searches": rows}

    # ---------- papers ----------

    @agent.tool_plain
    def list_papers(
        status: str | None = None,
        title_grep: str | None = None,
        min_citations: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        sort: str = "recent",
        limit: int = 20,
    ) -> dict[str, Any]:
        """List papers with composable filters. Returns compact summaries."""
        order_by = {
            "recent": "first_seen_at DESC",
            "cited": "COALESCE(cited_by_count, 0) DESC",
            "seen_in": "seen_in DESC, COALESCE(cited_by_count, 0) DESC",
        }.get(sort, "first_seen_at DESC")
        with db.connect(paths.db_path()) as con:
            papers = db.list_papers(
                con, limit=limit, status=status, title_grep=title_grep,
                min_citations=min_citations, year_from=year_from, year_to=year_to,
                order_by=order_by,
            )
        return {
            "n": len(papers),
            "papers": [
                {"id": p.id, "title": p.title, "year": p.year, "status": p.status,
                 "cited_by_count": p.cited_by_count, "seen_in": p.seen_in}
                for p in papers
            ],
        }

    @agent.tool_plain
    def triage(limit: int = 20, min_citations: int | None = None) -> dict[str, Any]:
        """Top undecided (status='candidate') papers, citation-sorted, with abstract excerpts."""
        with db.connect(paths.db_path()) as con:
            counts = db.count_by_status(con)
            papers = db.list_papers(
                con, limit=limit, status="candidate", min_citations=min_citations,
                order_by="COALESCE(cited_by_count, 0) DESC",
            )
        return {
            "n_candidates": counts.get("candidate", 0),
            "papers": [
                {"id": p.id, "title": p.title, "year": p.year,
                 "cited_by_count": p.cited_by_count,
                 "abstract_excerpt": (p.abstract or "")[:240]}
                for p in papers
            ],
        }

    @agent.tool_plain
    def paper_show(paper_id: str) -> dict[str, Any] | None:
        """Full Paper record (authors, abstract, references, locations)."""
        with db.connect(paths.db_path()) as con:
            p = db.get_paper(con, paper_id)
        return p.model_dump(mode="json") if p else None

    @agent.tool_plain
    def paper_links(paper_id: str) -> dict[str, Any] | None:
        """All known hosting locations — best source for 'where can I get the PDF'."""
        with db.connect(paths.db_path()) as con:
            p = db.get_paper(con, paper_id)
        if not p:
            return None
        return {
            "id": p.id, "title": p.title, "doi": p.doi,
            "oa_status": p.oa_status,
            "landing_page_url": p.landing_page_url, "pdf_url": p.pdf_url,
            "locations": [loc.model_dump() for loc in p.locations],
        }

    # ---------- screening + snowball ----------

    @agent.tool_plain
    def screen_paper(paper_id: str, decision: str, note: str | None = None) -> dict[str, Any]:
        """Mark a paper as included/excluded/maybe/candidate. Optional note."""
        with db.connect(paths.db_path()) as con:
            ok = db.set_screening(con, paper_id, db.normalize_status(decision), note=note)
        return {"paper_id": paper_id, "ok": ok, "status": db.normalize_status(decision)}

    @agent.tool_plain
    def screen_batch(
        paper_ids: list[str], decision: str, note: str | None = None,
    ) -> dict[str, Any]:
        """Apply the same decision to many papers at once."""
        normalized = db.normalize_status(decision)
        n_ok = 0
        with db.connect(paths.db_path()) as con:
            for pid in paper_ids:
                if db.set_screening(con, pid, normalized, note=note):
                    n_ok += 1
        return {"n": n_ok, "status": normalized}

    @agent.tool_plain
    def snowball_paper(
        paper_id: str, direction: str = "both", max_per_direction: int = 25,
    ) -> dict[str, Any]:
        """Expand a seed paper's citation neighbors (backward / forward / both)."""
        r = _snowball(paper_id, direction=direction, max_per_direction=max_per_direction)
        return {
            "seed": r.seed_paper_id, "direction": r.direction,
            "n_fetched": r.n_fetched, "n_new": r.n_new,
            "skipped_reason": r.skipped_reason,
        }

    # ---------- semantic search ----------

    @agent.tool_plain
    def semantic_query(text: str, k: int = 10, status: str | None = None) -> dict[str, Any]:
        """Cosine search over the embedded corpus."""
        hits = _query_corpus(text, k=max(k * 3, k))
        score_by_id = {h.paper_id: h.score for h in hits}
        with db.connect(paths.db_path()) as con:
            ids = list(score_by_id.keys())
            if not ids:
                return {"n": 0, "hits": []}
            ph = ",".join(["?"] * len(ids))
            sql = f"SELECT id, title, year, status FROM paper WHERE id IN ({ph})"
            args = list(ids)
            if status:
                sql += " AND status = ?"
                args.append(status)
            rows = con.execute(sql, args).fetchall()
        out = sorted(
            ({"id": r["id"], "title": r["title"], "year": r["year"],
              "status": r["status"], "score": round(score_by_id[r["id"]], 4)}
             for r in rows),
            key=lambda x: x["score"], reverse=True,
        )[:k]
        return {"n": len(out), "hits": out}

    @agent.tool_plain
    def papers_similar(paper_id: str, k: int = 10) -> dict[str, Any]:
        """Find papers most similar to a seed (cosine over embeddings)."""
        try:
            hits = _similar_to_paper(paper_id, k=k)
        except ValueError as e:
            return {"error": str(e)}
        return {"hits": [{"paper_id": h.paper_id, "score": round(h.score, 4)} for h in hits]}

    # ---------- pipeline ----------

    @agent.tool_plain
    def embed_corpus(only_missing: bool = True) -> dict[str, Any]:
        """Embed papers (sentence-transformers/all-MiniLM-L6-v2, 384d, local)."""
        r = _embed_papers(only_missing=only_missing)
        return {"n_embedded": r.n_embedded, "n_skipped": r.n_skipped, "elapsed_sec": round(r.elapsed_sec, 3)}

    @agent.tool_plain
    def project_corpus(method: str = "umap") -> dict[str, Any]:
        """Compute 2D projection of embedded papers (UMAP default, PCA fallback)."""
        r = _project_corpus(method=method)
        return {"n_points": r.n_points, "method": r.method, "note": r.note}

    @agent.tool_plain
    def refresh() -> dict[str, Any]:
        """One-shot: embed missing → project → re-render canvas. Run after search/snowball."""
        e = _embed_papers(only_missing=True)
        p = _project_corpus()
        if p.note:
            return {"embed": e.__dict__, "project": {"note": p.note}}
        papers_dir = paths.papers_dir()
        oc = _canvas_mod.write_obsidian_canvas(papers_dir / "landscape.canvas")
        hv = _canvas_mod.write_html_viewer(papers_dir / "landscape.html")
        return {
            "embed": {"n_embedded": e.n_embedded, "n_skipped": e.n_skipped},
            "project": {"n_points": p.n_points, "method": p.method},
            "canvas": {"obsidian_path": oc.get("path"), "html_path": hv.get("path")},
        }

    @agent.tool_plain
    def export_bibtex(status: str | None = "included") -> dict[str, Any]:
        """Export papers to a BibTeX file. Defaults to included papers → papers/refs.bib."""
        target = paths.papers_dir() / "refs.bib"
        r = _export_bibtex(target, status_filter=status)
        return {"path": r.path, "n_entries": r.n_entries}

    return agent


async def stream_chat(prompt: str) -> AsyncIterator[dict[str, str]]:
    """Yield SSE events: {event, data} dicts."""
    if not _has_api_key():
        yield {
            "event": "message",
            "data": json.dumps({
                "role": "assistant",
                "content": (
                    "Agent is offline — no `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) "
                    "set. The chat panel works; the agent itself needs a key. "
                    "Direct UI actions (search, screen, snowball, etc.) keep working."
                ),
            }),
        }
        yield {"event": "done", "data": "{}"}
        return

    try:
        agent = _build_agent()
    except Exception as e:
        yield {"event": "error", "data": json.dumps({"error": str(e)})}
        return

    try:
        async with agent.run_stream(prompt) as run:
            async for chunk in run.stream_text(delta=True):
                yield {"event": "delta", "data": json.dumps({"delta": chunk})}
        yield {"event": "done", "data": "{}"}
    except Exception as e:
        yield {"event": "error", "data": json.dumps({"error": str(e)})}
