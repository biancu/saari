"""Export the project corpus to manuscript-ready formats.

v1: BibTeX (what `paper/` will cite). Keys follow the Better BibTeX convention:
`<firstauthorlastname><year><firsttitleword>`, normalized to ASCII lowercase.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from saari import db, paths
from saari.models import Paper

_NONWORD = re.compile(r"[^a-z0-9]+")
_BIBTEX_ESCAPES = str.maketrans({
    "{": r"\{",
    "}": r"\}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
})

_ABSTRACT_MAX = 1500  # typical journal abstract upper bound; anything bigger is front-matter pollution
_ABSTRACT_NOISE_MARKERS = (
    "CrossrefMedlineGoogle",
    "Search for more papers by this author",
    "Download Citations",
    "Track Citations",
    "Disclosure Information",
)


def _clean_abstract(s: str | None) -> str | None:
    if not s:
        return None
    if any(marker in s for marker in _ABSTRACT_NOISE_MARKERS):
        return None
    s = s.strip()
    if len(s) > _ABSTRACT_MAX:
        # Take the first full sentence boundary before the cap, else hard truncate.
        head = s[:_ABSTRACT_MAX]
        last_period = head.rfind(". ")
        if last_period > _ABSTRACT_MAX * 0.5:
            head = head[: last_period + 1]
        s = head.rstrip() + " …"
    return s


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = _NONWORD.sub("", s)
    return s


def _citation_key(paper: Paper) -> str:
    parts: list[str] = []
    if paper.authors:
        surname = paper.authors[0].name.strip().split()[-1] if paper.authors[0].name else ""
        if surname:
            parts.append(_slug(surname))
    if paper.year is not None:
        parts.append(str(paper.year))
    if paper.title:
        first_word = paper.title.strip().split()[0]
        parts.append(_slug(first_word)[:14])
    key = "".join(parts).strip() or _slug(paper.id) or "unknown"
    return key


def _escape(s: str) -> str:
    return s.translate(_BIBTEX_ESCAPES) if s else ""


def _format_authors(paper: Paper) -> str:
    names = [a.name.strip() for a in paper.authors if a.name]
    if not names:
        return ""
    return " and ".join(_escape(n) for n in names)


def _entry_type(paper: Paper) -> str:
    if paper.venue:
        return "article"
    return "misc"


def _to_bibtex(paper: Paper) -> str:
    entry_type = _entry_type(paper)
    key = _citation_key(paper)
    fields: list[tuple[str, str]] = []
    if paper.title:
        fields.append(("title", _escape(paper.title)))
    authors = _format_authors(paper)
    if authors:
        fields.append(("author", authors))
    if paper.year is not None:
        fields.append(("year", str(paper.year)))
    if paper.venue:
        fields.append(("journal", _escape(paper.venue)))
    if paper.doi:
        fields.append(("doi", paper.doi))
    url = paper.landing_page_url or paper.pdf_url or (
        f"https://doi.org/{paper.doi}" if paper.doi else None
    )
    if url:
        fields.append(("url", url))
    clean_abs = _clean_abstract(paper.abstract) if not paper.abstract_suspect else None
    if clean_abs:
        fields.append(("abstract", _escape(clean_abs)))

    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{body}\n}}"


@dataclass
class ExportResult:
    path: str
    n_entries: int
    format: str


def export_bibtex(
    out: Path,
    status_filter: str | None = "included",
    project_root: Path | None = None,
) -> ExportResult:
    """Export papers to a BibTeX file. Default: included papers only."""
    root = project_root or paths.project_root()
    with db.connect(paths.db_path(root)) as con:
        papers = db.list_papers(
            con,
            limit=10_000,
            status=status_filter,
            order_by="year ASC NULLS LAST, first_seen_at ASC",
        )
    entries = [_to_bibtex(p) for p in papers]
    out.parent.mkdir(parents=True, exist_ok=True)
    header_bits = [
        f"% BibTeX export from saaristo project @ {root}",
    ]
    if status_filter:
        header_bits.append(f"% status filter: {status_filter}")
    header_bits.append(f"% {len(entries)} entries")
    out.write_text("\n".join(header_bits) + "\n\n" + "\n\n".join(entries) + "\n")
    return ExportResult(path=str(out), n_entries=len(entries), format="bibtex")


# --------------------------------------------------------------------------- #
# Tabular export (CSV / XLSX): a flat spreadsheet of the corpus, one row per
# paper. Handy for screening in Excel or sharing the raw hit list.
# --------------------------------------------------------------------------- #

# (header, Paper attribute) pairs; drives both CSV and XLSX so they stay in sync.
_TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "id"),
    ("doi", "doi"),
    ("title", "title"),
    ("year", "year"),
    ("venue", "venue"),
    ("authors", "authors"),
    ("cited_by", "cited_by_count"),
    ("status", "status"),
    ("seen_in", "seen_in"),
    ("url", "url"),
    ("abstract", "abstract"),
)

_TABLE_WIDTHS = {
    "id": 18, "doi": 22, "title": 60, "year": 6, "venue": 28, "authors": 40,
    "cited_by": 9, "status": 12, "seen_in": 8, "url": 40, "abstract": 80,
}


def _table_row(paper: Paper) -> list[object]:
    authors = "; ".join(a.name.strip() for a in paper.authors if a.name)
    url = paper.landing_page_url or paper.pdf_url or (
        f"https://doi.org/{paper.doi}" if paper.doi else ""
    )
    return [
        paper.id,
        paper.doi or "",
        paper.title or "",
        paper.year if paper.year is not None else "",
        paper.venue or "",
        authors,
        paper.cited_by_count if paper.cited_by_count is not None else "",
        paper.status,
        paper.seen_in,
        url,
        paper.abstract or "",
    ]


def _corpus(status_filter: str | None, root: Path) -> list[Paper]:
    with db.connect(paths.db_path(root)) as con:
        return db.list_papers(
            con,
            limit=1_000_000,
            status=status_filter,
            order_by="cited_by_count DESC NULLS LAST, year DESC NULLS LAST",
        )


def export_csv(
    out: Path,
    status_filter: str | None = None,
    project_root: Path | None = None,
) -> ExportResult:
    """Export the corpus to a CSV spreadsheet. Default: all papers.

    Written as UTF-8 with a BOM so Excel opens non-ASCII text (e.g. "ș")
    correctly on Windows.
    """
    import csv

    root = project_root or paths.project_root()
    papers = _corpus(status_filter, root)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([h for h, _ in _TABLE_COLUMNS])
        for p in papers:
            w.writerow(_table_row(p))
    return ExportResult(path=str(out), n_entries=len(papers), format="csv")


def export_xlsx(
    out: Path,
    status_filter: str | None = None,
    project_root: Path | None = None,
) -> ExportResult:
    """Export the corpus to an .xlsx workbook. Default: all papers.

    Bold frozen header row, autofilter, and sized columns.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ModuleNotFoundError as e:  # pragma: no cover - openpyxl is a declared dep
        raise RuntimeError(
            "xlsx export needs openpyxl. Reinstall saari (openpyxl is a declared "
            "dependency), or install it with `pip install openpyxl`. CSV export "
            "(`saari export csv`) has no such dependency."
        ) from e

    root = project_root or paths.project_root()
    papers = _corpus(status_filter, root)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "papers"
    ws.append([h for h, _ in _TABLE_COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for p in papers:
        ws.append(_table_row(p))
    ws.freeze_panes = "A2"
    if papers:
        ws.auto_filter.ref = ws.dimensions
    for idx, (header, _) in enumerate(_TABLE_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = _TABLE_WIDTHS.get(header, 16)
    wb.save(out)
    return ExportResult(path=str(out), n_entries=len(papers), format="xlsx")


# --------------------------------------------------------------------------- #
# SLR report artifacts (PRISMA diagram, manuscript, slides, one-shot bundle).
# `report` is imported lazily inside each function: report.py imports
# `_citation_key` from this module, so a top-level import would be circular.
# --------------------------------------------------------------------------- #

def _figures(root: Path, out_dir: Path) -> None:
    """Write the two shared figures (`prisma.svg`, `landscape.svg`) into out_dir."""
    from saari import report

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prisma.svg").write_text(report.render_prisma_svg(report.prisma_data(root)))
    (out_dir / "landscape.svg").write_text(
        report.render_landscape_svg(report.landscape_points(root))
    )


def export_prisma(
    out: Path, fmt: str = "svg", project_root: Path | None = None
) -> ExportResult:
    """Render the PRISMA 2020 flow diagram. `fmt`: 'svg' (default) | 'mermaid'."""
    from saari import report

    root = project_root or paths.project_root()
    data = report.prisma_data(root)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "mermaid":
        out.write_text(report.render_prisma_mermaid(data))
        fmt_label = "prisma.mmd"
    else:
        out.write_text(report.render_prisma_svg(data))
        fmt_label = "prisma.svg"
    return ExportResult(path=str(out), n_entries=data["n_included"], format=fmt_label)


def export_paper(out: Path, project_root: Path | None = None) -> ExportResult:
    """Assemble the Markdown SLR manuscript scaffold.

    Writes `paper.md` plus its companions (`prisma.svg`, `landscape.svg`,
    `refs.bib`) into the same directory. Mechanical content is filled;
    synthesis is left as `<!-- WRITE -->` slots.
    """
    from saari import report, study

    root = project_root or paths.project_root()
    out = Path(out)
    out_dir = out.parent
    prisma = report.prisma_data(root)
    funnel = study.funnel(root)
    themes = report.theme_groups(root, status="included")
    with db.connect(paths.db_path(root)) as con:
        included = db.list_papers(
            con, limit=1_000_000, status="included",
            order_by="year ASC NULLS LAST, first_seen_at ASC",
        )
    md = report.build_paper_md(study.get(root), prisma, funnel["searches"], themes, included)
    _figures(root, out_dir)
    export_bibtex(out_dir / "refs.bib", status_filter="included", project_root=root)
    out.write_text(md)
    return ExportResult(path=str(out), n_entries=len(included), format="paper.md")


def export_slides(out: Path, project_root: Path | None = None) -> ExportResult:
    """Assemble the Marp slide deck (writes companion figures alongside)."""
    from saari import report, study

    root = project_root or paths.project_root()
    out = Path(out)
    prisma = report.prisma_data(root)
    themes = report.theme_groups(root, status="included")
    md = report.build_slides_md(study.get(root), prisma, themes)
    _figures(root, out.parent)
    out.write_text(md)
    n = sum(len(v) for v in themes.values())
    return ExportResult(path=str(out), n_entries=n, format="slides.md")


def export_slr(out_dir: Path | None = None, project_root: Path | None = None) -> ExportResult:
    """One-shot: emit the whole review bundle (paper + slides + figures + bib).

    This is the headless entry point — a harness calls it to turn a screened
    corpus into `papers/review/{paper.md, slides.md, prisma.svg, landscape.svg,
    prisma.mmd, refs.bib}`.
    """
    root = project_root or paths.project_root()
    target = Path(out_dir) if out_dir else (paths.papers_dir(root) / "review")
    target.mkdir(parents=True, exist_ok=True)
    export_prisma(target / "prisma.mmd", fmt="mermaid", project_root=root)
    paper = export_paper(target / "paper.md", project_root=root)  # also writes svgs + refs.bib
    export_slides(target / "slides.md", project_root=root)
    return ExportResult(path=str(target), n_entries=paper.n_entries, format="slr-bundle")
