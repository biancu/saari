"""Bibliographic sources and shared search persistence.

`run_search` is the one entry point every surface (CLI, MCP, HTTP, in-app
agent) calls: it dispatches to the named source, dedupes across sources by
DOI, upserts, and records the search event. Keeping it here means a new
source lands on all surfaces by being added to `SOURCES`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from saari import db, paths
from saari.models import Paper

SOURCE_NAMES = ("openalex", "scopus")


def _search_source(
    source: str,
    query: str,
    *,
    limit: int,
    year_from: int | None,
    year_to: int | None,
    project_root: Path | None,
) -> list[tuple[Paper, str]]:
    if source == "openalex":
        from saari.sources import openalex

        return openalex.search(
            query, limit=limit, year_from=year_from, year_to=year_to, project_root=project_root
        )
    if source == "scopus":
        from saari.sources import scopus

        return scopus.search(
            query, limit=limit, year_from=year_from, year_to=year_to, project_root=project_root
        )
    raise ValueError(f"unknown source {source!r}; available: {', '.join(SOURCE_NAMES)}")


def run_search(
    query: str,
    *,
    source: str = "openalex",
    limit: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Search `source`, persist results, record the search event.

    Cross-source dedup: a fetched paper whose DOI already exists in the
    corpus under another id (e.g. Scopus result already found via OpenAlex)
    is not inserted again - the search links to the existing record, which
    counts toward `seen_in` and `n_duplicate`.
    """
    root = project_root or paths.project_root()
    fetched = _search_source(
        source, query, limit=limit, year_from=year_from, year_to=year_to, project_root=root
    )

    paper_ids: list[str] = []
    n_new = 0
    n_duplicate = 0
    with db.connect(paths.db_path(root)) as con:
        for paper, raw_path in fetched:
            existing = con.execute(
                "SELECT id FROM paper WHERE id = ?", (paper.id,)
            ).fetchone()
            if existing is None and paper.doi:
                row = con.execute(
                    "SELECT id FROM paper WHERE lower(doi) = ?", (paper.doi.lower(),)
                ).fetchone()
                if row is not None:
                    # Same work, found earlier via another source.
                    n_duplicate += 1
                    paper_ids.append(row["id"])
                    continue
            if existing is None:
                n_new += 1
            else:
                n_duplicate += 1
            db.upsert_paper(con, paper, raw_path=raw_path)
            paper_ids.append(paper.id)
        search_id = db.record_search(
            con,
            source=source,
            query=query,
            params={"limit": limit, "year_from": year_from, "year_to": year_to},
            paper_ids=paper_ids,
        )

    return {
        "search_id": search_id,
        "source": source,
        "query": query,
        "n_fetched": len(fetched),
        "n_new": n_new,
        "n_duplicate": n_duplicate,
        "paper_ids": paper_ids,
    }
