"""Integration test: build a tiny real project and run the SLR exporters.

Exercises the DB-backed wrappers (prisma_data, theme_groups, landscape_points)
and the file-writing entry points end-to-end, asserting the bundle is complete
and the manuscript's numbers reconcile with the screening funnel.
"""

import json

import pytest

from saari import db, export, paths, report, study
from saari.models import Author, Paper


def _paper(wid: str, title: str, year: int, cited: int) -> Paper:
    return Paper(
        id=f"openalex:{wid}", title=title, year=year, venue="Venue",
        cited_by_count=cited, doi=f"10.1/{wid}", authors=[Author(name=f"Author {wid}")],
    )


@pytest.fixture()
def project(tmp_path):
    """A minimal, fully-formed saaristo project with screened papers + topics."""
    root = paths.init_project(tmp_path)
    raw_dir = paths.raw_dir(root, "openalex")
    raw_dir.mkdir(parents=True, exist_ok=True)

    papers = [
        _paper("W1", "Retrieval-Augmented Generation Survey", 2023, 500),
        _paper("W2", "GraphRAG for Question Answering", 2024, 120),
        _paper("W3", "Knowledge Graph Embeddings", 2022, 300),
        _paper("W4", "An Off-Topic Paper About Bananas", 2019, 5),
    ]
    # OpenAlex-style topics for two papers (drives thematic grouping)
    topics = {
        "W1": [{"display_name": "Topic Modeling", "field": {"display_name": "Computer Science"}}],
        "W2": [{"display_name": "Semantic Web and Ontologies", "field": {"display_name": "Computer Science"}}],
    }

    with db.connect(paths.db_path(root)) as con:
        for p in papers:
            wid = p.id.split(":")[1]
            raw_rel = None
            if wid in topics:
                raw_file = raw_dir / f"{wid}.json"
                raw_file.write_text(json.dumps({"id": p.id, "topics": topics[wid]}))
                raw_rel = str(raw_file.relative_to(root))
            db.upsert_paper(con, p, raw_path=raw_rel)
            # give everything a projection point so landscape renders
            con.execute(
                "INSERT INTO paper_projection (paper_id, x, y, method, params_json) VALUES (?,?,?,?,?)",
                (p.id, hash(p.id) % 10, (hash(p.id) // 10) % 10, "test", "{}"),
            )
        db.record_search(con, "openalex", "rag kg", {}, ["openalex:W1", "openalex:W2", "openalex:W3"])
        db.record_search(con, "snowball", "backward:openalex:W1", {}, ["openalex:W4"])
        db.set_screening(con, "openalex:W1", "included", "on-topic")
        db.set_screening(con, "openalex:W2", "included", "on-topic")
        db.set_screening(con, "openalex:W4", "excluded", "off-topic: not about RAG")
        # W3 left as candidate

    study.update(
        project_root=root, title="Test SLR", authors="Tester",
        question="RQ?", criteria="include RAG+KG",
    )
    return root


def test_prisma_data_reconciles_with_screening(project):
    d = report.prisma_data(project)
    assert d["identification"]["n_database"] == 3
    assert d["identification"]["n_snowball"] == 1
    assert d["n_included"] == 2
    assert d["excluded"]["n"] == 1
    assert "off-topic: not about RAG" in d["excluded"]["reasons"]
    assert d["n_unscreened"] == 1  # W3


def test_theme_groups_use_openalex_topics(project):
    groups = report.theme_groups(project, status="included")
    assert set(groups) == {"Topic Modeling", "Semantic Web and Ontologies"}


def test_export_csv_all_papers(project, tmp_path):
    import csv

    out = tmp_path / "corpus.csv"
    r = export.export_csv(out, project_root=project)
    assert r.format == "csv"
    assert r.n_entries == 4  # all statuses, no filter
    with out.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4
    assert rows[0]["title"] == "Retrieval-Augmented Generation Survey"  # most-cited first
    assert rows[0]["cited_by"] == "500"
    assert rows[0]["authors"] == "Author W1"
    assert "status" in rows[0] and "url" in rows[0]


def test_export_csv_status_filter(project, tmp_path):
    import csv

    out = tmp_path / "included.csv"
    r = export.export_csv(out, status_filter="included", project_root=project)
    assert r.n_entries == 2
    with out.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert {row["id"] for row in rows} == {"openalex:W1", "openalex:W2"}


def test_export_xlsx_all_papers(project, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")

    out = tmp_path / "corpus.xlsx"
    r = export.export_xlsx(out, project_root=project)
    assert r.format == "xlsx"
    assert r.n_entries == 4
    assert out.exists()
    wb = openpyxl.load_workbook(out)
    ws = wb.active
    assert ws.title == "papers"
    assert [c.value for c in ws[1]][:3] == ["id", "doi", "title"]
    assert ws.max_row == 5  # header + 4 papers
    assert ws.freeze_panes == "A2"


def test_export_slr_writes_full_bundle(project):
    r = export.export_slr(project_root=project)
    out = paths.papers_dir(project) / "review"
    assert r.n_entries == 2
    for fname in ["paper.md", "slides.md", "prisma.svg", "prisma.mmd", "landscape.svg", "refs.bib"]:
        f = out / fname
        assert f.exists() and f.stat().st_size > 0, f"missing/empty: {fname}"

    paper = (out / "paper.md").read_text()
    assert "# Test SLR" in paper
    assert "> RQ?" in paper
    assert "Studies included" not in paper or "n=2" in report.render_prisma_svg(report.prisma_data(project))
    # included papers present by cite key; the candidate (W3) and excluded (W4) are not in the reference list
    assert "Retrieval-Augmented Generation Survey" in paper
    assert "Bananas" not in paper  # excluded paper must not leak into the manuscript body
    # honesty: limitations disclosed, synthesis left as slots
    assert "Single bibliographic source" in paper
    assert "<!-- WRITE:" in paper


def test_export_slides_marp_and_bibtex_included_only(project):
    export.export_slides(paths.papers_dir(project) / "slides.md", project_root=project)
    slides = (paths.papers_dir(project) / "slides.md").read_text()
    assert slides.startswith("---\nmarp: true")

    r = export.export_bibtex(paths.papers_dir(project) / "refs.bib", status_filter="included", project_root=project)
    assert r.n_entries == 2
