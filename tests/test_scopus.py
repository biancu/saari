"""Tests for the Scopus source and cross-source DOI dedup. No network."""

from __future__ import annotations

import json

import httpx
import pytest

from saari import db, paths
from saari.models import Paper
from saari.sources import run_search
from saari.sources import scopus


def _entry(scopus_id="85001", title="A Scopus Paper", doi="10.1/xyz", **over):
    e = {
        "dc:identifier": f"SCOPUS_ID:{scopus_id}",
        "eid": f"2-s2.0-{scopus_id}",
        "dc:title": title,
        "dc:creator": "Ada L.",
        "prism:publicationName": "Journal of Tests",
        "prism:coverDate": "2024-05-01",
        "prism:doi": doi,
        "citedby-count": "7",
        "link": [{"@ref": "scopus", "@href": "https://www.scopus.com/record/85001"}],
    }
    e.update(over)
    return e


def _mock_transport(payloads):
    """Return an httpx MockTransport serving canned search pages in order."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = payloads[min(calls["n"], len(payloads) - 1)]
        calls["n"] += 1
        if isinstance(payload, int):
            return httpx.Response(payload, json={"service-error": {"status": {"statusText": "canned"}}})
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _patch_client(monkeypatch, payloads):
    transport = _mock_transport(payloads)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(scopus.httpx, "Client", fake_client)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = paths.init_project(tmp_path)
    monkeypatch.setenv("SCOPUS_API_KEY", "k")
    monkeypatch.delenv("SCOPUS_INST_TOKEN", raising=False)
    return root


def _page(entries, total=None):
    return {
        "search-results": {
            "opensearch:totalResults": str(total if total is not None else len(entries)),
            "entry": entries,
        }
    }


def test_entry_maps_to_paper():
    p = scopus._entry_to_paper(_entry())
    assert p is not None
    assert p.id == "scopus:85001"
    assert p.doi == "10.1/xyz"
    assert p.year == 2024
    assert p.venue == "Journal of Tests"
    assert p.cited_by_count == 7
    assert p.authors[0].name == "Ada L."
    assert p.abstract is None  # STANDARD view has no abstract
    assert "scopus.com" in (p.landing_page_url or "")


def test_search_persists_and_dumps_raw(project, monkeypatch):
    _patch_client(monkeypatch, [_page([_entry()])])
    result = run_search("agents", source="scopus", limit=10, project_root=project)
    assert result["source"] == "scopus"
    assert result["n_fetched"] == 1 and result["n_new"] == 1
    assert result["paper_ids"] == ["scopus:85001"]
    raw = paths.raw_dir(project, "scopus") / "85001.json"
    assert raw.exists()
    assert json.loads(raw.read_text())["dc:title"] == "A Scopus Paper"
    with db.connect(paths.db_path(project)) as con:
        assert db.get_paper(con, "scopus:85001") is not None


def test_doi_dedup_across_sources(project, monkeypatch):
    # Same work already in the corpus via OpenAlex, different id, same DOI.
    with db.connect(paths.db_path(project)) as con:
        db.upsert_paper(con, Paper(id="openalex:W1", title="Same work", doi="10.1/xyz"))
    _patch_client(monkeypatch, [_page([_entry(doi="10.1/XYZ")])])  # case differs
    result = run_search("agents", source="scopus", limit=10, project_root=project)
    assert result["n_new"] == 0
    assert result["n_duplicate"] == 1
    assert result["paper_ids"] == ["openalex:W1"]  # linked, not re-inserted
    with db.connect(paths.db_path(project)) as con:
        assert db.get_paper(con, "scopus:85001") is None


def test_empty_result_entry(project, monkeypatch):
    _patch_client(monkeypatch, [_page([{"error": "Result set was empty"}], total=0)])
    result = run_search("nothing", source="scopus", limit=10, project_root=project)
    assert result["n_fetched"] == 0


def test_pagination(project, monkeypatch):
    page1 = _page([_entry(scopus_id=str(85000 + i)) for i in range(25)], total=30)
    page2 = _page([_entry(scopus_id=str(85100 + i)) for i in range(5)], total=30)
    _patch_client(monkeypatch, [page1, page2])
    result = run_search("agents", source="scopus", limit=30, project_root=project)
    assert result["n_fetched"] == 30


def test_auth_error_is_diagnosed(project, monkeypatch):
    _patch_client(monkeypatch, [401])
    with pytest.raises(scopus.ScopusError) as exc:
        run_search("agents", source="scopus", limit=5, project_root=project)
    msg = str(exc.value)
    assert "InstToken" in msg and "IP range" in msg


def test_missing_key_is_clear(project, monkeypatch):
    monkeypatch.delenv("SCOPUS_API_KEY")
    with pytest.raises(scopus.ScopusError, match="SCOPUS_API_KEY"):
        run_search("agents", source="scopus", limit=5, project_root=project)


def test_unknown_source(project):
    with pytest.raises(ValueError, match="unknown source"):
        run_search("agents", source="wos", limit=5, project_root=project)


def _capture_query(monkeypatch):
    """Patch the Scopus client and capture the outgoing `query` param."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.params.get("query", "")
        return httpx.Response(200, json=_page([_entry()]))

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(scopus.httpx, "Client", fake_client)
    return seen


def test_subjarea_filter_appended(project, monkeypatch):
    seen = _capture_query(monkeypatch)
    run_search(
        "agents", source="scopus", limit=5, subjareas=["COMP", "ENGI", "MATH"], project_root=project
    )
    q = seen["query"]
    # SUBJAREA is a top-level field code: it must sit OUTSIDE TITLE-ABS-KEY.
    assert q == (
        "TITLE-ABS-KEY(agents) AND "
        "(SUBJAREA(COMP) OR SUBJAREA(ENGI) OR SUBJAREA(MATH))"
    )


def test_no_subjarea_leaves_query_untouched(project, monkeypatch):
    seen = _capture_query(monkeypatch)
    run_search("agents", source="scopus", limit=5, project_root=project)
    assert seen["query"] == "TITLE-ABS-KEY(agents)"
