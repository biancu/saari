"""Unit tests for the SLR report layer — pure functions, no DB or network.

These cover the logic that assembles PRISMA counts, themes, tables, and the
manuscript/slides scaffold, plus the two honesty guarantees: counts reconcile,
and interpretive content is left as `<!-- WRITE -->` slots (never fabricated).
"""

from saari import report
from saari.export import _citation_key
from saari.models import Author, Paper

FUNNEL = {
    "searches": [
        {"source": "openalex", "query": "rag kg", "n_returned": 30, "created_at": "2026-07-01T10:00:00"},
        {"source": "openalex", "query": "graphrag", "n_returned": 20, "created_at": "2026-07-01T10:01:00"},
        {"source": "snowball", "query": "backward:W1", "n_returned": 28, "created_at": "2026-07-01T10:05:00"},
    ],
    "n_fetched": 78,
    "n_unique": 60,
    "by_status": {"included": 10, "excluded": 8, "maybe": 2, "candidate": 40},
}


def _excluded(n: int) -> list[Paper]:
    return [
        Paper(id=f"openalex:W{i}", title=f"P{i}", screening_note="off-topic" if i % 2 else "wrong domain")
        for i in range(n)
    ]


def test_build_prisma_reconciles():
    d = report.build_prisma(FUNNEL, _excluded(8))
    # identification split by source
    assert d["identification"]["n_database"] == 50  # 30 + 20 openalex
    assert d["identification"]["n_snowball"] == 28
    assert d["identification"]["n_total"] == 78
    # dedup: fetched - unique
    assert d["n_duplicates_removed"] == 18
    assert d["n_unique"] == 60
    # screened = included + excluded + maybe
    assert d["n_screened"] == 20
    assert d["n_unscreened"] == 40
    assert d["n_included"] == 10
    assert d["excluded"]["n"] == 8
    # reasons bucketed from screening_note
    assert sum(d["excluded"]["reasons"].values()) == 8
    assert set(d["excluded"]["reasons"]) == {"off-topic", "wrong domain"}


def test_excluded_without_note_gets_placeholder():
    d = report.build_prisma(FUNNEL, [Paper(id="openalex:W1", title="x")])
    assert "no reason recorded" in d["excluded"]["reasons"]


def test_prisma_svg_and_mermaid_wellformed():
    d = report.build_prisma(FUNNEL, _excluded(8))
    svg = report.render_prisma_svg(d)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "Studies included (n=10)" in svg
    mmd = report.render_prisma_mermaid(d)
    assert mmd.startswith("flowchart TD")
    assert "n=10" in mmd


def test_group_by_topic_orders_by_size_uncategorized_last():
    ps = [Paper(id=str(i), title=f"P{i}") for i in range(4)]
    labels = {"0": "Retrieval", "1": "Retrieval", "2": "Graphs", "3": None}
    groups = report.group_by_topic(ps, labels)
    keys = list(groups)
    assert keys[0] == "Retrieval"  # largest first
    assert keys[-1] == "Uncategorized"  # missing -> last
    assert len(groups["Retrieval"]) == 2


def test_primary_topic_prefers_specific_topic_over_field():
    topics = [{"display_name": "Semantic Web and Ontologies",
               "subfield": {"display_name": "Artificial Intelligence"},
               "field": {"display_name": "Computer Science"}}]
    assert report._primary_topic_label(topics) == "Semantic Web and Ontologies"
    assert report._primary_topic_label([]) is None


def test_characteristics_table_is_mechanical():
    p = Paper(id="openalex:W1", title="A Study", year=2024, venue="NeurIPS",
              cited_by_count=42, doi="10.1/x", oa_status="gold",
              authors=[Author(name="Jane Doe")])
    tbl = report.characteristics_table([p])
    assert "| Cite key |" in tbl
    assert f"`{_citation_key(p)}`" in tbl
    assert "2024" in tbl and "NeurIPS" in tbl and "42" in tbl


def test_reference_keys_match_bibtex_citation_key():
    p = Paper(id="openalex:W1", title="A Study", year=2024,
              authors=[Author(name="Jane Doe")])
    refs = report.reference_list([p])
    assert f"[`{_citation_key(p)}`]" in refs


def test_limitations_disclose_method_bounds():
    block = report.limitations_block()
    for must in ["Single bibliographic source", "OpenAlex", "Single-reviewer",
                 "Title/abstract screening", "risk-of-bias", "PROSPERO"]:
        assert must in block


def test_paper_md_has_all_sections_slots_and_no_fabrication():
    study_rec = {"title": "RAG+KG Review", "authors": "Me",
                 "question": "How do KGs help RAG?", "criteria": "include X"}
    prisma = report.build_prisma(FUNNEL, _excluded(8))
    inc = [Paper(id="openalex:W1", title="Paper One", year=2024, venue="NeurIPS",
                 cited_by_count=100, doi="10.1/x", authors=[Author(name="Jane Doe")])]
    themes = {"Retrieval": inc}
    md = report.build_paper_md(study_rec, prisma, FUNNEL["searches"], themes, inc)
    for must in ["# RAG+KG Review", "## Abstract", "## 1. Introduction",
                 "## 2. Methods", "### 2.2 Information sources", "## 3. Results",
                 "### 3.3 Study characteristics", "## References",
                 "How do KGs help RAG?", "Single bibliographic source"]:
        assert must in md, f"missing section/content: {must}"
    # the RQ appears verbatim (not paraphrased/invented)
    assert "> How do KGs help RAG?" in md
    # every interpretive section is an explicit authoring slot, not invented prose
    assert md.count("<!-- WRITE:") >= 6
    # included paper reachable by its cite key
    assert f"`{_citation_key(inc[0])}`" in md


def test_slides_are_marp():
    prisma = report.build_prisma(FUNNEL, _excluded(8))
    sl = report.build_slides_md({"title": "T", "question": "Q", "authors": ""}, prisma, {})
    assert sl.startswith("---\nmarp: true")
    assert "## Research question" in sl and "## Method" in sl


def test_landscape_svg_handles_empty_and_points():
    assert "No projection" in report.render_landscape_svg([])
    svg = report.render_landscape_svg([{"x": 0, "y": 0, "status": "included", "title": "T", "cited": 10}])
    assert svg.startswith("<svg") and "circle" in svg


def test_build_prisma_papers_outside_searches():
    """Corpus larger than search returns must not make dedup go negative.

    Papers added outside recorded searches (direct by id, seeds) go to an
    "other" bucket so identified >= unique and duplicates removed is exact.
    """
    funnel = {
        "searches": [
            {"source": "openalex", "query": "q", "n_returned": 207, "created_at": "2026-07-01"},
        ],
        "n_fetched": 207,
        "n_unique": 208,
        "by_status": {"included": 43, "excluded": 57, "maybe": 22, "candidate": 86},
    }
    d = report.build_prisma(funnel, [])
    assert d["identification"]["n_other"] == 1
    assert d["identification"]["n_total"] == 208
    assert d["n_duplicates_removed"] == 0
    assert d["identification"]["n_total"] >= d["n_unique"]
    mmd = report.render_prisma_mermaid(d)
    assert "other: 1" in mmd
    svg = report.render_prisma_svg(d)
    assert "other: 1" in svg


def test_limitations_sources_bullet_is_factual():
    single = report.limitations_block({"openalex": 80, "snowball": 20})
    assert "Single bibliographic source" in single
    multi = report.limitations_block({"openalex": 80, "scopus": 40, "snowball": 20})
    assert "Single bibliographic source" not in multi
    assert "2 databases (openalex, scopus)" in multi
    # No source info at all: keep the conservative single-source wording.
    assert "Single bibliographic source" in report.limitations_block(None)
