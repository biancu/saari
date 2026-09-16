"""HTTP API + SPA host for the saari corpus.

Mirrors the MCP tool surface as REST endpoints under `/api`, and serves the
built React SPA from `ui/dist/` at the root. One server, two skins.

Routes:
    GET  /api/health
    GET  /api/project
    GET  /api/papers                 (filters, pagination)
    GET  /api/papers/{id}
    GET  /api/papers/{id}/links
    GET  /api/papers/{id}/raw
    GET  /api/papers/{id}/similar
    POST /api/papers/{id}/screen
    POST /api/papers/{id}/snowball
    GET  /api/searches
    POST /api/searches               (openalex or scopus, DOI-deduped)
    GET  /api/searches/{id}/results
    GET  /api/projection
    POST /api/embed
    POST /api/project-corpus
    POST /api/refresh
    POST /api/query
    GET  /api/agent/stream           (SSE — Pydantic AI chat)

Static SPA mounted last at `/` with html=True so deep links work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

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
from saari.projection import (
    get_projection as _get_projection,
    project_corpus as _project_corpus,
)
from saari.snowball import snowball as _snowball


# ---------- response shapes ----------


class PaperCard(BaseModel):
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
    landing_page_url: str | None = None
    pdf_url: str | None = None


def _to_card(p: Paper, include_excerpt: bool = True) -> PaperCard:
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
        landing_page_url=p.landing_page_url,
        pdf_url=p.pdf_url,
    )


# ---------- request bodies ----------


class ScreenIn(BaseModel):
    decision: str
    note: str | None = None


class ScreenBatchIn(BaseModel):
    paper_ids: list[str]
    decision: str
    note: str | None = None


class SnowballIn(BaseModel):
    direction: str = "both"
    max_per_direction: int = 25


class SearchIn(BaseModel):
    query: str
    limit: int = 25
    year_from: int | None = None
    year_to: int | None = None
    source: str = "openalex"


class QueryIn(BaseModel):
    text: str
    k: int = 20
    status: str | None = None


class EmbedIn(BaseModel):
    only_missing: bool = True


class ProjectIn(BaseModel):
    method: str = "umap"
    n_neighbors: int = 15
    min_dist: float = 0.1


class StudyIn(BaseModel):
    title: str | None = None
    authors: str | None = None
    question: str | None = None
    criteria: str | None = None
    tags: list[str] | None = None


class ReviewFileIn(BaseModel):
    content: str


# The review bundle's files, with their served content-type. This doubles as the
# path-safety allow-list: only these bare basenames are ever read or written.
REVIEW_FILES: dict[str, str] = {
    "paper.md": "text/markdown; charset=utf-8",
    "slides.md": "text/markdown; charset=utf-8",
    "prisma.svg": "image/svg+xml",
    "landscape.svg": "image/svg+xml",
    "prisma.mmd": "text/plain; charset=utf-8",
    "refs.bib": "text/plain; charset=utf-8",
}
REVIEW_EDITABLE: set[str] = {"paper.md", "slides.md"}


# ---------- app factory ----------


class ProjectCreate(BaseModel):
    name: str


def create_app() -> FastAPI:
    app = FastAPI(title="saari", version="0.0.1")

    # Vite dev server proxy still works without CORS, but allow it for direct dev.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Hosted (multi-user) mode: when SAARI_DATA_ROOT is set, scope every
    # request to the caller's project directory. No-op locally.
    from saari.hosting import RequestRootMiddleware

    app.add_middleware(RequestRootMiddleware)

    @app.exception_handler(paths.ProjectNotFoundError)
    async def _no_project(_: Request, exc: paths.ProjectNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": "ProjectNotFoundError", "message": str(exc),
                     "hint": "Run `saari init` in a directory first."},
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/projects")
    def projects_list(request: Request) -> dict[str, Any]:
        from saari import hosting

        base = hosting.data_root()
        if base is None:
            root = paths.project_root()
            return {"hosted": False, "projects": [root.name], "active": root.name}
        user = next(
            (request.headers.get(h) for h in hosting.USER_HEADERS if request.headers.get(h)),
            None,
        )
        if user is None:
            raise HTTPException(401, "missing identity header")
        active = request.headers.get(hosting.PROJECT_HEADER) or hosting.DEFAULT_PROJECT
        return {
            "hosted": True,
            "projects": hosting.list_projects(base / user),
            "active": active,
        }

    @app.post("/api/projects")
    def projects_create(body: ProjectCreate, request: Request) -> dict[str, Any]:
        from saari import hosting

        base = hosting.data_root()
        if base is None:
            raise HTTPException(400, "project creation is only available in hosted mode")
        user = next(
            (request.headers.get(h) for h in hosting.USER_HEADERS if request.headers.get(h)),
            None,
        )
        if user is None:
            raise HTTPException(401, "missing identity header")
        try:
            root = hosting.resolve_hosted_root(
                {hosting.USER_HEADERS[1]: user, hosting.PROJECT_HEADER: body.name}
            )
        except ValueError:
            raise HTTPException(400, "invalid project name") from None
        assert root is not None
        return {"name": body.name, "root": str(root)}

    @app.get("/api/project")
    def project_info() -> dict[str, Any]:
        root = paths.project_root()
        with db.connect(paths.db_path(root)) as con:
            n_papers = con.execute("SELECT COUNT(*) FROM paper").fetchone()[0]
            n_searches = con.execute("SELECT COUNT(*) FROM search").fetchone()[0]
            by_status = db.count_by_status(con)
            n_embedded = con.execute("SELECT COUNT(*) FROM paper_embedding_meta").fetchone()[0]
            n_projected = con.execute("SELECT COUNT(*) FROM paper_projection").fetchone()[0]
        return {
            "root": str(root),
            "name": root.name,
            "db": str(paths.db_path(root)),
            "raw_dir": str(paths.raw_dir(root)),
            "papers_dir": str(paths.papers_dir(root)),
            "n_papers": n_papers,
            "n_searches": n_searches,
            "by_status": by_status,
            "pipeline": {"embedded": n_embedded, "projected": n_projected},
        }

    # ---------- papers ----------

    @app.get("/api/papers")
    def list_papers(
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_citations: int | None = None,
        title_grep: str | None = None,
        seen_in_at_least: int | None = None,
        sort: str = "recent",
    ) -> dict[str, Any]:
        order_by = {
            "recent": "first_seen_at DESC",
            "cited": "COALESCE(cited_by_count, 0) DESC",
            "seen_in": "seen_in DESC, COALESCE(cited_by_count, 0) DESC",
        }.get(sort, "first_seen_at DESC")

        root = paths.project_root()
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
            total = con.execute("SELECT COUNT(*) FROM paper").fetchone()[0]
        return {
            "n": len(papers),
            "total": total,
            "papers": [_to_card(p).model_dump() for p in papers],
        }

    @app.get("/api/papers/{paper_id:path}")
    def get_paper(paper_id: str) -> dict[str, Any]:
        root = paths.project_root()
        with db.connect(paths.db_path(root)) as con:
            p = db.get_paper(con, paper_id)
        if not p:
            raise HTTPException(404, f"Paper not found: {paper_id}")
        return p.model_dump(mode="json")

    @app.get("/api/papers/{paper_id:path}/links")
    def paper_links(paper_id: str) -> dict[str, Any]:
        root = paths.project_root()
        with db.connect(paths.db_path(root)) as con:
            p = db.get_paper(con, paper_id)
        if not p:
            raise HTTPException(404, f"Paper not found: {paper_id}")
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

    @app.get("/api/papers/{paper_id:path}/raw")
    def paper_raw(paper_id: str) -> dict[str, Any]:
        root = paths.project_root()
        with db.connect(paths.db_path(root)) as con:
            raw_path = db.get_raw_path(con, paper_id)
        if not raw_path:
            raise HTTPException(404, "No raw payload")
        full = root / raw_path
        if not full.exists():
            raise HTTPException(404, "Raw file missing on disk")
        return {"path": str(full), "content": json.loads(full.read_text())}

    @app.get("/api/papers/{paper_id:path}/similar")
    def papers_similar(paper_id: str, k: int = 10, status: str | None = None) -> dict[str, Any]:
        root = paths.project_root()
        try:
            hits = _similar_to_paper(paper_id, k=max(k * 3, k), project_root=root)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

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
                {**_to_card(p).model_dump(), "score": round(score_by_id[p.id], 4)}
                for p in papers
            ],
        }

    @app.post("/api/screen-batch")
    def screen_batch(body: ScreenBatchIn) -> dict[str, Any]:
        root = paths.project_root()
        normalized = db.normalize_status(body.decision)
        n_ok = 0
        with db.connect(paths.db_path(root)) as con:
            for pid in body.paper_ids:
                if db.set_screening(con, pid, normalized, note=body.note):
                    n_ok += 1
        return {"n": n_ok, "status": normalized}

    @app.post("/api/papers/{paper_id:path}/screen")
    def screen(paper_id: str, body: ScreenIn) -> dict[str, Any]:
        root = paths.project_root()
        normalized = db.normalize_status(body.decision)
        with db.connect(paths.db_path(root)) as con:
            ok = db.set_screening(con, paper_id, normalized, note=body.note)
        if not ok:
            raise HTTPException(404, f"Paper not found: {paper_id}")
        return {"paper_id": paper_id, "status": normalized, "ok": ok}

    @app.post("/api/papers/{paper_id:path}/snowball")
    def snowball(paper_id: str, body: SnowballIn) -> dict[str, Any]:
        root = paths.project_root()
        r = _snowball(
            paper_id,
            direction=body.direction,
            max_per_direction=body.max_per_direction,
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

    # ---------- searches ----------

    @app.get("/api/searches")
    def searches_list(limit: int = 50) -> dict[str, Any]:
        root = paths.project_root()
        with db.connect(paths.db_path(root)) as con:
            rows = db.list_searches(con, limit=limit)
        return {"searches": rows}

    @app.post("/api/searches")
    def run_search_endpoint(body: SearchIn) -> dict[str, Any]:
        from saari.sources import run_search
        from saari.sources.scopus import ScopusError

        try:
            return run_search(
                body.query,
                source=body.source,
                limit=body.limit,
                year_from=body.year_from,
                year_to=body.year_to,
                project_root=paths.project_root(),
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except ScopusError as e:
            raise HTTPException(502, str(e)) from None

    @app.get("/api/searches/{search_id}/results")
    def search_results(search_id: int) -> dict[str, Any]:
        root = paths.project_root()
        with db.connect(paths.db_path(root)) as con:
            row = con.execute(
                "SELECT * FROM search WHERE id = ?", (search_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Search not found")
            paper_rows = con.execute(
                "SELECT p.*, "
                "(SELECT COUNT(DISTINCT search_id) FROM search_result sr2 WHERE sr2.paper_id = p.id) AS seen_in "
                "FROM paper p JOIN search_result sr ON sr.paper_id = p.id "
                "WHERE sr.search_id = ? ORDER BY sr.rank ASC",
                (search_id,),
            ).fetchall()
            papers = [db._row_to_paper(r) for r in paper_rows]
        return {
            "search": dict(row),
            "papers": [_to_card(p).model_dump() for p in papers],
        }

    # ---------- projection / embeddings ----------

    @app.get("/api/projection")
    def projection() -> dict[str, Any]:
        root = paths.project_root()
        coords = _get_projection(project_root=root)
        if not coords:
            return {"n": 0, "points": []}
        # join with paper data
        ids = [c["paper_id"] for c in coords]
        with db.connect(paths.db_path(root)) as con:
            placeholders = ",".join(["?"] * len(ids))
            rows = con.execute(
                "SELECT p.*, "
                "(SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
                f"FROM paper p WHERE p.id IN ({placeholders})",
                ids,
            ).fetchall()
            paper_by_id = {r["id"]: db._row_to_paper(r) for r in rows}

        points = []
        for c in coords:
            p = paper_by_id.get(c["paper_id"])
            if not p:
                continue
            card = _to_card(p, include_excerpt=False).model_dump()
            points.append({**card, "x": c["x"], "y": c["y"]})
        return {"n": len(points), "points": points}

    @app.post("/api/embed")
    def embed(body: EmbedIn) -> dict[str, Any]:
        r = _embed_papers(only_missing=body.only_missing, project_root=paths.project_root())
        return {
            "n_embedded": r.n_embedded,
            "n_skipped": r.n_skipped,
            "model": r.model,
            "dim": r.dim,
            "elapsed_sec": round(r.elapsed_sec, 3),
        }

    @app.post("/api/project-corpus")
    def project_corpus(body: ProjectIn) -> dict[str, Any]:
        r = _project_corpus(
            method=body.method,
            n_neighbors=body.n_neighbors,
            min_dist=body.min_dist,
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

    @app.post("/api/refresh")
    def refresh() -> dict[str, Any]:
        root = paths.project_root()
        e = _embed_papers(only_missing=True, project_root=root)
        p = _project_corpus(project_root=root)
        if p.note:
            return {"embed": e.__dict__, "project": {"note": p.note}, "canvas": None}
        papers_dir = paths.papers_dir(root)
        oc = _canvas_mod.write_obsidian_canvas(papers_dir / "landscape.canvas", project_root=root)
        hv = _canvas_mod.write_html_viewer(papers_dir / "landscape.html", project_root=root)
        return {
            "embed": {"n_embedded": e.n_embedded, "n_skipped": e.n_skipped},
            "project": {"n_points": p.n_points, "method": p.method, "x_range": p.x_range, "y_range": p.y_range},
            "canvas": {"obsidian": oc, "html": hv},
        }

    @app.post("/api/query")
    def query(body: QueryIn) -> dict[str, Any]:
        root = paths.project_root()
        hits = _query_corpus(body.text, k=max(body.k * 3, body.k), project_root=root)
        if not hits:
            return {"query": body.text, "n": 0, "papers": [], "note": "No embedded papers."}
        score_by_id = {h.paper_id: h.score for h in hits}
        with db.connect(paths.db_path(root)) as con:
            placeholders = ",".join(["?"] * len(hits))
            sql = (
                "SELECT p.*, "
                "(SELECT COUNT(DISTINCT search_id) FROM search_result sr WHERE sr.paper_id = p.id) AS seen_in "
                f"FROM paper p WHERE p.id IN ({placeholders})"
            )
            args: list[Any] = [h.paper_id for h in hits]
            if body.status is not None:
                sql += " AND p.status = ?"
                args.append(body.status)
            rows = con.execute(sql, args).fetchall()
            papers = sorted(
                (db._row_to_paper(r) for r in rows),
                key=lambda p: score_by_id.get(p.id, 0.0),
                reverse=True,
            )[: body.k]
        return {
            "query": body.text,
            "n": len(papers),
            "papers": [
                {**_to_card(p).model_dump(), "score": round(score_by_id[p.id], 4)}
                for p in papers
            ],
        }

    # ---------- study (research question + criteria) ----------

    @app.get("/api/study")
    def study_get() -> dict[str, Any]:
        return _study.get(project_root=paths.project_root())

    @app.put("/api/study")
    def study_put(body: StudyIn) -> dict[str, Any]:
        return _study.update(
            title=body.title,
            authors=body.authors,
            question=body.question,
            criteria=body.criteria,
            tags=body.tags,
            project_root=paths.project_root(),
        )

    # ---------- PRISMA-style funnel ----------

    @app.get("/api/funnel")
    def funnel() -> dict[str, Any]:
        return _study.funnel(project_root=paths.project_root())

    @app.get("/api/stages")
    def stages() -> dict[str, Any]:
        from saari.status import stages_data

        return stages_data(project_root=paths.project_root())

    # ---------- citation edges (intra-corpus) ----------

    @app.get("/api/edges")
    def edges() -> dict[str, Any]:
        """Pairs (source, target) where both papers are in the corpus and
        source.referenced_works contains target. Used by the UMAP overlay.
        """
        root = paths.project_root()
        with db.connect(paths.db_path(root)) as con:
            rows = con.execute(
                "SELECT id, referenced_works_json FROM paper"
            ).fetchall()
        id_set = {r["id"] for r in rows}
        edges_list: list[dict[str, str]] = []
        for r in rows:
            try:
                refs = json.loads(r["referenced_works_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            for ref in refs:
                full = ref if ref.startswith("openalex:") else f"openalex:{ref}"
                if full in id_set and full != r["id"]:
                    edges_list.append({"source": r["id"], "target": full})
        return {"n": len(edges_list), "edges": edges_list}

    # ---------- export ----------

    @app.post("/api/export/bibtex")
    def export_bibtex(status: str | None = "included") -> dict[str, Any]:
        root = paths.project_root()
        target = paths.papers_dir(root) / "refs.bib"
        r = _export_bibtex(target, status_filter=status, project_root=root)
        return {"path": r.path, "n_entries": r.n_entries, "format": r.format}

    @app.post("/api/export/prisma")
    def export_prisma(fmt: str = "svg") -> dict[str, Any]:
        root = paths.project_root()
        ext = "mmd" if fmt == "mermaid" else "svg"
        target = paths.papers_dir(root) / f"prisma.{ext}"
        r = _export_prisma(target, fmt=fmt, project_root=root)
        return {"path": r.path, "n_entries": r.n_entries, "format": r.format}

    @app.post("/api/export/paper")
    def export_paper() -> dict[str, Any]:
        root = paths.project_root()
        r = _export_paper(paths.papers_dir(root) / "paper.md", project_root=root)
        return {"path": r.path, "n_entries": r.n_entries, "format": r.format}

    @app.post("/api/export/slides")
    def export_slides() -> dict[str, Any]:
        root = paths.project_root()
        r = _export_slides(paths.papers_dir(root) / "slides.md", project_root=root)
        return {"path": r.path, "n_entries": r.n_entries, "format": r.format}

    @app.post("/api/export/slr")
    def export_slr() -> dict[str, Any]:
        root = paths.project_root()
        r = _export_slr(None, project_root=root)
        return {"path": r.path, "n_entries": r.n_entries, "format": r.format}

    # ---------- review bundle: list / serve / edit / download ----------

    def _review_dir() -> Path:
        return paths.papers_dir(paths.project_root()) / "review"

    def _review_target(name: str) -> Path:
        # `name` is a path param without `:path`, so it can never contain `/`;
        # the allow-list rejects anything that is not a known bundle file.
        if name not in REVIEW_FILES:
            raise HTTPException(status_code=404, detail=f"unknown review file: {name!r}")
        return _review_dir() / name

    @app.get("/api/review")
    def review_list() -> dict[str, Any]:
        d = _review_dir()
        files = [
            {
                "name": name,
                "size": (d / name).stat().st_size,
                "format": ctype,
                "editable": name in REVIEW_EDITABLE,
            }
            for name, ctype in REVIEW_FILES.items()
            if (d / name).exists()
        ]
        return {"exists": bool(files), "dir": str(d), "files": files}

    @app.get("/api/review/file/{name}")
    def review_file_get(name: str, download: bool = False) -> Response:
        target = _review_target(name)
        if not target.exists():
            raise HTTPException(
                status_code=404,
                detail=f"{name} not generated yet — run export first.",
            )
        headers = (
            {"Content-Disposition": f'attachment; filename="{name}"'} if download else {}
        )
        return Response(
            content=target.read_bytes(), media_type=REVIEW_FILES[name], headers=headers
        )

    @app.put("/api/review/file/{name}")
    def review_file_put(name: str, body: ReviewFileIn) -> dict[str, Any]:
        if name not in REVIEW_EDITABLE:
            raise HTTPException(status_code=400, detail=f"{name} is not editable")
        target = _review_target(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content)
        return {"name": name, "ok": True, "size": target.stat().st_size}

    # ---------- agent (SSE) ----------

    @app.get("/api/agent/stream")
    async def agent_stream(prompt: str = Query(...)) -> EventSourceResponse:
        from saari.agent import stream_chat
        return EventSourceResponse(stream_chat(prompt))

    # ---------- static SPA ----------

    # Packaged wheel ships the built SPA at saari/_ui; a source checkout
    # serves ui/dist instead.
    _pkg_ui = Path(__file__).resolve().parent / "_ui"
    _repo_ui = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"
    ui_dist = _pkg_ui if _pkg_ui.exists() else _repo_ui
    if ui_dist.exists():
        # Serve hashed assets (immutable cache, no fallback) under /assets/
        assets_dir = ui_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        index_path = ui_dist / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            from fastapi.responses import FileResponse, HTMLResponse
            # Try a literal file first (favicon.svg, robots.txt, etc.).
            candidate = ui_dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(str(candidate))
            # Read index.html fresh each request so rebuilds show up without restart.
            return HTMLResponse(index_path.read_text())
    else:
        @app.get("/")
        def index() -> dict[str, Any]:
            return {
                "ok": True,
                "note": "UI not built. Run `cd ui && pnpm install && pnpm run build`, "
                "or `pnpm run dev` for hot reload (vite proxies /api here).",
            }

    return app


app = create_app()
