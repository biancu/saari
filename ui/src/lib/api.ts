// Typed API client for the saari HTTP server.
// Vite proxies /api → http://127.0.0.1:3044 in dev; same-origin in prod.

export type ScreenStatus = "candidate" | "included" | "excluded" | "maybe";

export interface PaperCard {
  id: string;
  title: string;
  year: number | null;
  cited_by_count: number | null;
  venue: string | null;
  doi: string | null;
  arxiv_id: string | null;
  oa_status: string | null;
  status: ScreenStatus;
  seen_in: number;
  abstract_suspect: boolean;
  abstract_excerpt: string | null;
  landing_page_url: string | null;
  pdf_url: string | null;
  score?: number;
}

export interface ProjectionPoint extends PaperCard {
  x: number;
  y: number;
}

export interface Author {
  name: string;
  orcid?: string | null;
  affiliation?: string | null;
}

export interface PaperFull extends PaperCard {
  abstract: string | null;
  authors: Author[];
  pmid: string | null;
  pmcid: string | null;
  referenced_works: string[];
  related_works: string[];
  screening_note: string | null;
  source_provenance: Record<string, unknown>;
  first_seen_at: string | null;
  last_updated_at: string | null;
  locations: Array<{
    source_name: string | null;
    landing_page_url: string | null;
    pdf_url: string | null;
    version: string | null;
    license: string | null;
    is_oa: boolean;
  }>;
}

export interface ProjectInfo {
  root: string;
  name: string;
  db: string;
  raw_dir: string;
  papers_dir: string;
  n_papers: number;
  n_searches: number;
  by_status: Record<string, number>;
  pipeline: { embedded: number; projected: number };
}

export interface SearchRow {
  id: number;
  source: string;
  query: string;
  params_json: string;
  n_returned: number | null;
  created_at: string;
}

export interface Study {
  question: string;
  criteria: string;
  tags: string[];
  started_at: string | null;
  updated_at: string | null;
}

export interface Funnel {
  n_searches: number;
  searches: SearchRow[];
  n_fetched: number;
  n_unique: number;
  n_screened: number;
  n_unscreened: number;
  by_status: Record<string, number>;
  embedded: number;
  projected: number;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

async function put<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

async function getText(path: string): Promise<string> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
  return r.text();
}

export interface Stage {
  key: string;
  label: string;
  state: "done" | "active" | "todo";
  detail: string;
  hint: string;
  view: string;
}

export interface Stages {
  stages: Stage[];
  next: string | null;
}

export interface ReviewFile {
  name: string;
  size: number;
  format: string;
  editable: boolean;
}

export const api = {
  health: () => get<{ ok: boolean }>("/api/health"),
  project: () => get<ProjectInfo>("/api/project"),
  stages: () => get<Stages>("/api/stages"),

  papers: (params: Record<string, string | number | boolean | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
    }
    const qs = q.toString();
    return get<{ n: number; total: number; papers: PaperCard[] }>(
      `/api/papers${qs ? `?${qs}` : ""}`,
    );
  },
  paper: (id: string) => get<PaperFull>(`/api/papers/${encodeURIComponent(id)}`),
  paperLinks: (id: string) =>
    get<{
      id: string;
      title: string;
      doi: string | null;
      landing_page_url: string | null;
      pdf_url: string | null;
      locations: PaperFull["locations"];
    }>(`/api/papers/${encodeURIComponent(id)}/links`),
  paperSimilar: (id: string, k = 10) =>
    get<{ seed: string; n: number; papers: PaperCard[] }>(
      `/api/papers/${encodeURIComponent(id)}/similar?k=${k}`,
    ),
  screen: (id: string, decision: ScreenStatus, note?: string) =>
    post<{ paper_id: string; status: string; ok: boolean }>(
      `/api/papers/${encodeURIComponent(id)}/screen`,
      { decision, note },
    ),
  screenBatch: (paper_ids: string[], decision: ScreenStatus, note?: string) =>
    post<{ n: number; status: string }>("/api/screen-batch", { paper_ids, decision, note }),
  snowball: (id: string, direction: "backward" | "forward" | "both" = "both", max = 25) =>
    post<{ seed: string; n_fetched: number; n_new: number }>(
      `/api/papers/${encodeURIComponent(id)}/snowball`,
      { direction, max_per_direction: max },
    ),

  searches: () => get<{ searches: SearchRow[] }>("/api/searches"),
  searchResults: (id: number) =>
    get<{ search: SearchRow; papers: PaperCard[] }>(`/api/searches/${id}/results`),
  runSearch: (body: { query: string; limit?: number; year_from?: number; year_to?: number; source?: string }) =>
    post<{
      search_id: number;
      n_fetched: number;
      n_new: number;
      n_duplicate: number;
      paper_ids: string[];
    }>("/api/searches", body),

  projection: () => get<{ n: number; points: ProjectionPoint[] }>("/api/projection"),
  embed: (only_missing = true) =>
    post<{ n_embedded: number; n_skipped: number; elapsed_sec: number }>("/api/embed", {
      only_missing,
    }),
  projectCorpus: (method = "umap") =>
    post<{ n_points: number; method: string }>("/api/project-corpus", { method }),
  refresh: () =>
    post<{ embed: unknown; project: unknown; canvas: unknown }>("/api/refresh"),
  query: (body: { text: string; k?: number; status?: string }) =>
    post<{ query: string; n: number; papers: PaperCard[] }>("/api/query", body),

  study: () => get<Study>("/api/study"),
  studyUpdate: (body: Partial<Pick<Study, "question" | "criteria" | "tags">>) =>
    put<Study>("/api/study", body),

  funnel: () => get<Funnel>("/api/funnel"),

  exportBibtex: (status: string | null = "included") =>
    post<{ path: string; n_entries: number; format: string }>(
      `/api/export/bibtex${status ? `?status=${encodeURIComponent(status)}` : ""}`,
    ),

  // ---- SLR review bundle: generate, list, read, edit, download ----
  exportSlr: () =>
    post<{ path: string; n_entries: number; format: string }>("/api/export/slr"),
  reviewList: () =>
    get<{ exists: boolean; dir: string; files: ReviewFile[] }>("/api/review"),
  reviewFile: (name: string) =>
    getText(`/api/review/file/${encodeURIComponent(name)}`),
  saveReviewFile: (name: string, content: string) =>
    put<{ name: string; ok: boolean; size: number }>(
      `/api/review/file/${encodeURIComponent(name)}`,
      { content },
    ),
  reviewFileUrl: (name: string, opts?: { download?: boolean }) =>
    `/api/review/file/${encodeURIComponent(name)}${opts?.download ? "?download=1" : ""}`,

  edges: () =>
    get<{ n: number; edges: Array<{ source: string; target: string }> }>("/api/edges"),
};
