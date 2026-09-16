import { useEffect, useState } from "react";
import { api, type PaperCard, type SearchRow } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PaperRow } from "@/components/PaperRow";
import { PaperDetail } from "@/components/PaperDetail";
import { Search as SearchIcon, Loader2 } from "lucide-react";

export function Searches() {
  const [rows, setRows] = useState<SearchRow[]>([]);
  const [selected, setSelected] = useState<SearchRow | null>(null);
  const [results, setResults] = useState<PaperCard[]>([]);
  const [paperId, setPaperId] = useState<string | null>(null);

  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(25);
  const [yearFrom, setYearFrom] = useState<string>("");
  const [yearTo, setYearTo] = useState<string>("");
  const [source, setSource] = useState("openalex");
  const [running, setRunning] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  function refresh() {
    return api.searches().then((r) => setRows(r.searches));
  }

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    if (!selected) {
      setResults([]);
      return;
    }
    api.searchResults(selected.id).then((r) => setResults(r.papers));
  }, [selected]);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!q.trim() || running) return;
    setRunning(true);
    setMsg(null);
    try {
      const r = await api.runSearch({
        query: q.trim(),
        limit,
        year_from: yearFrom ? Number(yearFrom) : undefined,
        year_to: yearTo ? Number(yearTo) : undefined,
        source,
      });
      setMsg(
        `fetched ${r.n_fetched} (new ${r.n_new}, dup ${r.n_duplicate}) — search #${r.search_id}`,
      );
      await refresh();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="flex flex-1 min-w-0 min-h-0">
      <div className="w-72 border-r border-zinc-800 flex flex-col">
        <div className="border-b border-zinc-800 p-3">
          <h2 className="text-sm font-semibold text-zinc-200 mb-2">New search</h2>
          <form onSubmit={run} className="space-y-2">
            <Input
              placeholder="query…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <select
              value={source}
              onChange={(e) => setSource(e.target.value)}
              title="Scopus needs SCOPUS_API_KEY on the server"
              className="w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-200 focus:outline-none focus:border-zinc-600"
            >
              <option value="openalex">OpenAlex (keyless)</option>
              <option value="scopus">Scopus (needs API key)</option>
            </select>
            <div className="flex gap-1.5">
              <Input
                placeholder="from"
                inputMode="numeric"
                value={yearFrom}
                onChange={(e) => setYearFrom(e.target.value)}
              />
              <Input
                placeholder="to"
                inputMode="numeric"
                value={yearTo}
                onChange={(e) => setYearTo(e.target.value)}
              />
              <Input
                placeholder="limit"
                inputMode="numeric"
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value) || 25)}
                className="w-20"
              />
            </div>
            <Button type="submit" disabled={running || !q.trim()} className="w-full">
              {running ? <Loader2 className="size-4 animate-spin" /> : <SearchIcon className="size-4" />}
              run
            </Button>
            {msg && (
              <div className="text-xs text-zinc-400 mt-1 leading-relaxed">{msg}</div>
            )}
          </form>
        </div>

        <div className="flex-1 overflow-y-auto">
          {rows.length === 0 && (
            <div className="p-4 text-xs text-zinc-500">No searches yet.</div>
          )}
          {rows.map((s) => (
            <button
              key={s.id}
              onClick={() => setSelected(s)}
              className={`block w-full text-left px-3 py-2 border-b border-zinc-800/60 transition-colors ${
                selected?.id === s.id
                  ? "bg-zinc-800/60"
                  : "hover:bg-zinc-900/60"
              }`}
            >
              <div className="text-sm text-zinc-100 truncate">{s.query}</div>
              <div className="text-[10px] text-zinc-500 mt-0.5 flex items-center gap-2">
                <span>#{s.id}</span>
                <span>{s.source}</span>
                <span>{s.n_returned ?? 0} results</span>
                <span className="ml-auto truncate">{s.created_at?.slice(0, 16)}</span>
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 min-w-0 flex flex-col">
        <div className="border-b border-zinc-800 px-4 py-2 flex items-center justify-between shrink-0">
          <div className="text-sm text-zinc-300">
            {selected ? (
              <>
                <span className="text-zinc-500">search:</span> {selected.query}{" "}
                <span className="text-zinc-600">— {results.length} results</span>
              </>
            ) : (
              <span className="text-zinc-500">Pick a search on the left, or run a new one.</span>
            )}
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-3 space-y-1.5">
          {results.map((p) => (
            <PaperRow
              key={p.id}
              paper={p}
              onSelect={(p) => setPaperId(p.id)}
              selected={paperId === p.id}
            />
          ))}
        </div>
      </div>

      {paperId && (
        <div className="w-96 shrink-0">
          <PaperDetail
            paperId={paperId}
            onClose={() => setPaperId(null)}
            onChange={() => {
              if (selected) api.searchResults(selected.id).then((r) => setResults(r.papers));
            }}
          />
        </div>
      )}
    </div>
  );
}
