"""Scopus (Elsevier) search source.

Uses the Scopus Search API (https://dev.elsevier.com). Access model, which
trips people up constantly:

- An API key from dev.elsevier.com is free to create and always "valid",
  but on its own it only grants access when the request comes from an IP
  range that Elsevier has registered as belonging to a *subscribing*
  institution. Being on a campus network is not enough if that campus's
  ranges are not registered for API access.
- Off-network (or from an unregistered network), the key must be paired
  with an institutional token ("InstToken") issued by Elsevier support to
  the subscribing institution's library.

Configuration (env):
- SCOPUS_API_KEY   - required.
- SCOPUS_INST_TOKEN - optional InstToken for off-network access.

The STANDARD view (what unentitled/basic access returns) has title, venue,
year, DOI, citation count and first author, but NO abstract. Papers arrive
with `abstract=None`; screening on Scopus-only records means fetching the
abstract elsewhere (a DOI hit on OpenAlex usually fills it).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from saari import paths
from saari.models import Author, Paper

BASE_URL = "https://api.elsevier.com/content/search/scopus"
# STANDARD view pages are capped at 25 entries per request.
PAGE_SIZE = 25


class ScopusError(RuntimeError):
    """Scopus request failed with a diagnosable cause."""


def _headers() -> dict[str, str]:
    key = os.environ.get("SCOPUS_API_KEY")
    if not key:
        raise ScopusError(
            "SCOPUS_API_KEY is not set. Create a key at https://dev.elsevier.com "
            "and export it. Off-network access also needs SCOPUS_INST_TOKEN "
            "(an InstToken from your library / Elsevier support)."
        )
    headers = {"X-ELS-APIKey": key, "Accept": "application/json"}
    inst = os.environ.get("SCOPUS_INST_TOKEN")
    if inst:
        headers["X-ELS-Insttoken"] = inst
    return headers


def _raise_for_auth(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        detail = ""
        try:
            body = resp.json()
            detail = (
                body.get("service-error", {}).get("status", {}).get("statusText")
                or body.get("error-response", {}).get("error-message")
                or ""
            )
        except Exception:  # noqa: BLE001 - body may be HTML/empty
            detail = resp.text[:200]
        raise ScopusError(
            f"Scopus refused the request ({resp.status_code}). A valid API key "
            "alone is not enough: requests must come from an IP range that "
            "Elsevier has registered for your institution's Scopus "
            "subscription, or carry an InstToken (set SCOPUS_INST_TOKEN). "
            "If you are on your institution's network and still see this, the "
            "institution's API entitlement or IP registration is likely "
            "missing - ask the library to request API access / an InstToken "
            f"from Elsevier. Server said: {detail!r}"
        )
    if resp.status_code == 429:
        raise ScopusError(
            "Scopus quota exceeded (429). Keys have weekly request quotas; "
            "wait for the reset or use another key."
        )
    resp.raise_for_status()


def _entry_to_paper(entry: dict[str, Any]) -> Paper | None:
    ident = entry.get("dc:identifier") or ""  # "SCOPUS_ID:85123..."
    scopus_id = ident.rsplit(":", 1)[-1] if ident else None
    title = entry.get("dc:title")
    if not scopus_id or not title:
        return None

    year: int | None = None
    cover = entry.get("prism:coverDate") or ""
    if len(cover) >= 4 and cover[:4].isdigit():
        year = int(cover[:4])

    authors: list[Author] = []
    # COMPLETE view carries an author array; STANDARD only dc:creator.
    for a in entry.get("author") or []:
        name = a.get("authname") or " ".join(
            x for x in (a.get("given-name"), a.get("surname")) if x
        )
        if name:
            authors.append(Author(name=name))
    if not authors and entry.get("dc:creator"):
        authors.append(Author(name=entry["dc:creator"]))

    landing = None
    for link in entry.get("link") or []:
        if link.get("@ref") == "scopus":
            landing = link.get("@href")
            break

    doi = (entry.get("prism:doi") or "").strip().lower() or None
    cites = entry.get("citedby-count")

    return Paper(
        id=f"scopus:{scopus_id}",
        doi=doi,
        pmid=(entry.get("pubmed-id") or None),
        title=title,
        abstract=entry.get("dc:description") or None,
        year=year,
        venue=entry.get("prism:publicationName") or None,
        authors=authors,
        cited_by_count=int(cites) if cites is not None else None,
        landing_page_url=landing,
        source_provenance={"scopus": {"eid": entry.get("eid"), "view": "search"}},
    )


def _dump_raw(entry: dict[str, Any], scopus_id: str, project_root: Path | None) -> str:
    dest = paths.raw_dir(project_root, "scopus") / f"{scopus_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
    return str(dest.relative_to(project_root or paths.project_root()))


def search(
    query: str,
    limit: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    subjareas: list[str] | None = None,
    project_root: Path | None = None,
) -> list[tuple[Paper, str]]:
    """Search Scopus TITLE-ABS-KEY. Returns [(paper, raw_path), ...].

    Persists each raw entry under `.saaristo/raw/scopus/<id>.json`.

    `subjareas` is an optional list of Scopus subject-area codes (e.g.
    ["COMP", "ENGI", "MATH"]). They are OR'd together and AND'ed onto the
    query *outside* the TITLE-ABS-KEY wrapper - `SUBJAREA` is a top-level
    field code and cannot be nested inside TITLE-ABS-KEY.
    """
    scopus_query = f"TITLE-ABS-KEY({query})"
    if subjareas:
        clause = " OR ".join(f"SUBJAREA({code})" for code in subjareas)
        scopus_query = f"{scopus_query} AND ({clause})"
    params: dict[str, Any] = {
        "query": scopus_query,
        "count": min(limit, PAGE_SIZE),
        "start": 0,
    }
    if year_from is not None or year_to is not None:
        hi = year_to or datetime.now().year + 1
        lo = year_from or 1800
        params["date"] = f"{lo}-{hi}"

    headers = _headers()
    out: list[tuple[Paper, str]] = []
    start = 0
    with httpx.Client(timeout=30.0) as client:
        while len(out) < limit:
            params["start"] = start
            params["count"] = min(limit - len(out), PAGE_SIZE)
            resp = client.get(BASE_URL, params=params, headers=headers)
            _raise_for_auth(resp)
            results = resp.json().get("search-results", {})
            entries = results.get("entry") or []
            # An empty result set comes back as one entry holding an "error".
            if not entries or (len(entries) == 1 and "error" in entries[0]):
                break
            start += len(entries)
            for entry in entries:
                paper = _entry_to_paper(entry)
                if paper is None:
                    continue
                raw = _dump_raw(entry, paper.id.split(":", 1)[1], project_root)
                out.append((paper, raw))
                if len(out) >= limit:
                    break
            total = int(results.get("opensearch:totalResults") or 0)
            if start >= total:
                break
    return out
