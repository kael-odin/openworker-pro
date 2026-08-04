// Browse Marketplace modal — opened from CustomizeView's "Browse Marketplace" button.
// Shows configured extension sources (DHP source list reused read-only) and a category
// tab strip mirroring the 7 Customize sections. Install affordances are wired only for
// categories whose backend is ready (E1+); the rest show "coming soon" inline.
//
// This is the marketplace *shell* — as E1-E5 land install APIs per category, each tab's
// empty state gets replaced with a real browse+install list. The source list is already
// live (DHP SourceManager), so users can see where extensions come from today.
import { useEffect, useState } from "react";
import {
  addPluginSource,
  addMcpSource,
  addPersonaSource,
  addSkillSource,
  getDhpSources,
  getMcpCatalog,
  getMcpSources,
  getPersonaCatalog,
  getPersonaSources,
  getPluginCatalog,
  getPluginSources,
  getSkillCatalog,
  getSkillSources,
  installMcpFromCatalog,
  installPlugin,
  installPersonaFromSource,
  installSkill,
  removeMcpSource,
  removePluginSource,
  removePersonaSource,
  removeSkillSource,
  resetMcpSources,
  resetPluginSources,
  resetSkillSources,
  updateMcpSource,
  updatePluginSource,
  updatePersonaSource,
  updateSkillSource,
  type DhpSource,
  type McpCatalogItem,
  type McpSource,
  type PersonaCatalogItem,
  type PersonaSource,
  type PluginCatalogItem,
  type PluginSource,
  type SkillCatalogItem,
  type SkillSource,
} from "../api";
import { Icon } from "./Icon";
import { useT } from "../i18n/I18nProvider";

type Cat =
  | "plugins"
  | "mcp"
  | "skills"
  | "subagents"
  | "rules"
  | "commands"
  | "hooks";

const CATS: { key: Cat; labelKey: string; icon: string }[] = [
  { key: "plugins", labelKey: "customize.plugins_title", icon: "puzzle" },
  { key: "mcp", labelKey: "customize.mcp_title", icon: "code" },
  { key: "skills", labelKey: "customize.skills_title", icon: "file" },
  { key: "subagents", labelKey: "customize.subagents_title", icon: "bot" },
  { key: "rules", labelKey: "customize.rules_title", icon: "listChecks" },
  { key: "commands", labelKey: "customize.commands_title", icon: "terminal" },
  { key: "hooks", labelKey: "customize.hooks_title", icon: "sliders" },
];

// ModelScope MCP plaza has 13 categories. The agg API has no category-list endpoint and its
// Criterion filter is ignored, so we hardcode the full set here (mirroring the site sidebar)
// rather than deriving from page data (which only surfaces the 7 hottest slugs). The backend
// (catalog.py) returns the same 13 slugs as the `categories` array; we render chips from this
// richer local list to get the Chinese label.
//
// NOTE: no per-category counts. The ModelScope agg API ignores Criterion/PageSize/PageNumber
// (it always returns the same 10 hot servers), so a category count from the site sidebar
// (e.g. "浏览器自动化 589") is unverifiable from this API and would mislead users into
// expecting 589 results when the query-based category filter returns far fewer. Selecting a
// category folds the slug into the agg `Query` (the only field the API honors), returning the
// servers that match that category — an honest, installable result set.
const MCP_CATEGORIES: { slug: string; zh: string }[] = [
  { slug: "browser-automation", zh: "浏览器自动化" },
  { slug: "search", zh: "搜索工具" },
  { slug: "communication-and-collaboration", zh: "交流协作工具" },
  { slug: "developer-tools", zh: "开发者工具" },
  { slug: "entertainment-and-media", zh: "娱乐与多媒体" },
  { slug: "file-system", zh: "文件系统" },
  { slug: "finance", zh: "金融" },
  { slug: "knowledge-management-and-memory", zh: "知识管理与记忆" },
  { slug: "location-services", zh: "位置服务" },
  { slug: "culture-and-art", zh: "文化与艺术" },
  { slug: "academic-research", zh: "学术研究" },
  { slug: "schedule-management", zh: "日程管理" },
  { slug: "other", zh: "其他" },
];

const MCP_CATEGORY_ZH: Record<string, string> = Object.fromEntries(
  MCP_CATEGORIES.map((c) => [c.slug, c.zh]),
);

function mcpCategoryLabel(slug: string): string {
  return MCP_CATEGORY_ZH[slug] || slug;
}

export function CustomizeMarketplace({ onClose }: { onClose: () => void }) {
  const { t } = useT();
  // Sources shown in the strip: skill sources on the skills tab (live, E1), DHP sources
  // elsewhere (read-only marketplace preview). Typed loosely since both share the same shape.
  const [sources, setSources] = useState<(DhpSource | SkillSource | McpSource)[]>([]);
  const [cat, setCat] = useState<Cat>("plugins");

  useEffect(() => {
    if (cat === "skills") {
      getSkillSources()
        .then((r) => setSources(r ?? []))
        .catch(() => setSources([]));
    } else if (cat === "plugins") {
      getPluginSources()
        .then((r) => setSources(r ?? []))
        .catch(() => setSources([]));
    } else if (cat === "mcp") {
      getMcpSources()
        .then((r) => setSources(r ?? []))
        .catch(() => setSources([]));
    } else if (cat === "subagents") {
      getPersonaSources()
        .then((r) => setSources(r ?? []))
        .catch(() => setSources([]));
    } else {
      getDhpSources()
        .then((r) => setSources(r.sources ?? []))
        .catch(() => setSources([]));
    }
  }, [cat]);

  // ESC closes (matches SystemPromptEditor / GalleryModal convention).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50" data-testid="customize-marketplace">
      <div
        className="absolute inset-0 bg-black/30 backdrop-blur-[1px]"
        onClick={onClose}
      />
      <div className="relative mx-auto mt-[6vh] w-[min(900px,92vw)] max-h-[82vh] flex flex-col rounded-xl2 border border-line bg-panel shadow-2xl">
        {/* Header */}
        <div className="flex items-center gap-3 px-5 py-3.5 border-b border-line">
          <Icon name="puzzle" size={17} className="text-accent shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="text-[14.5px] font-semibold">{t("customize.marketplace_title")}</div>
          </div>
          <button
            className="text-muted hover:text-ink p-1 rounded-lg hover:bg-paper"
            onClick={onClose}
            aria-label="Close"
          >
            <Icon name="x" size={16} />
          </button>
        </div>

        {/* Sources strip — read-only overview. The plugins tab has its own inline
            source CRUD (add/toggle/delete) inside PluginBrowseSection; the skills tab
            manages sources inside SkillBrowseSection. Other tabs show DHP sources here. */}
        <div className="px-5 py-3 border-b border-line">
          <div className="text-[11px] uppercase tracking-[0.05em] text-faint mb-2">
            {t("customize.marketplace_sources")}
          </div>
          {sources.length === 0 ? (
            <div className="text-[12.5px] text-faint">{t("customize.marketplace_empty")}</div>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {sources.map((s) => (
                <span
                  key={s.id}
                  className="text-[11.5px] px-2.5 py-1 rounded-full border border-line bg-paper text-muted flex items-center gap-1.5"
                  title={s.url}
                >
                  <span
                    className={
                      "w-1.5 h-1.5 rounded-full " +
                      (s.enabled ? "bg-accent" : "bg-faint")
                    }
                  />
                  {s.name}
                  {s.is_default && (
                    <span className="text-faint">· {t("customize.marketplace_source_default")}</span>
                  )}
                  {!s.is_default && (
                    <span className="text-faint">
                      · {s.source_type === "local"
                        ? t("customize.marketplace_source_local")
                        : t("customize.marketplace_source_online")}
                    </span>
                  )}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Category tabs */}
        <div className="px-5 py-2 border-b border-line flex flex-wrap gap-1">
          {CATS.map((c) => {
            const active = cat === c.key;
            return (
              <button
                key={c.key}
                className={
                  "text-[12px] px-2.5 py-1 rounded-full flex items-center gap-1.5 transition-colors " +
                  (active
                    ? "bg-accent text-white"
                    : "bg-paper border border-line text-muted hover:text-ink")
                }
                onClick={() => setCat(c.key)}
              >
                <Icon name={c.icon as any} size={12} /> {t(c.labelKey)}
              </button>
            );
          })}
        </div>

        {/* Body — per-category install list. Only categories with backend install
            support show real content; others show coming-soon (filled in E1-E5). */}
        <div className="flex-1 overflow-y-auto px-5 py-4 hairline-scroll">
          {cat === "plugins" ? (
            <PluginBrowseSection onInstalled={onClose} />
          ) : cat === "mcp" ? (
            <McpBrowseSection onInstalled={onClose} />
          ) : cat === "skills" ? (
            <SkillBrowseSection onInstalled={onClose} />
          ) : cat === "subagents" ? (
            <PersonaBrowseSection onInstalled={onClose} />
          ) : (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <Icon name="puzzle" size={28} className="text-faint mb-3" />
              <div className="text-[13px] font-medium text-muted mb-1">
                {t("customize.coming_soon")}
              </div>
              <div className="text-[12px] text-faint max-w-xs">
                {t("customize.note")}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Skills tab (E1): pick a source → browse its catalog → install. The catalog is fetched on
// demand (git sources clone on first browse, so the first click can take a few seconds). Each
// item shows whether it's already installed; install writes to state_dir()/skills/<name>/.
// Sources are fully manageable here (add/toggle/delete, mirroring PluginBrowseSection) — the
// backend + api.ts had full CRUD from the start, this wires it up.
//
// ModelScope sources (source_type="modelscope") return a paginated catalog (~76k items).
// For those we render a search box + page controls + icon/category/downloads per card.
// git/local/http sources return a flat list (no pagination) and degrade to the simple layout.
function SkillBrowseSection({ onInstalled }: { onInstalled: () => void }) {
  const { t } = useT();
  const [sources, setSources] = useState<SkillSource[]>([]);
  const [selId, setSelId] = useState<string>("");
  const [items, setItems] = useState<SkillCatalogItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [installing, setInstalling] = useState<string>("");
  const [justInstalled, setJustInstalled] = useState<string>("");
  // ModelScope pagination + search state (ignored for git/local/http sources).
  const [page, setPage] = useState(1);
  const [pageSize] = useState(50);
  const [totalCount, setTotalCount] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [categories, setCategories] = useState<string[]>([]);
  const [selCat, setSelCat] = useState<string>("__all");

  // Inline source-add form state.
  const [showAdd, setShowAdd] = useState(false);
  const [addName, setAddName] = useState("");
  const [addUrl, setAddUrl] = useState("");
  const [addType, setAddType] = useState("http");
  const [addBusy, setAddBusy] = useState(false);

  const selSource = sources.find((s) => s.id === selId);
  // modelscope + skillhub support server-side search (the query is forwarded to their
  // APIs and returns a fresh page). git/local/http sources return a small flat list with
  // no server-side search, so we filter that list client-side in `visible`.
  const isServerSearch = selSource?.source_type === "modelscope" || selSource?.source_type === "skillhub";
  // The text the user has typed into the search box. For server-search sources this is
  // committed to `searchQuery` (which triggers a refetch) on Enter/click. For client-search
  // sources `searchQuery` stays "" (no refetch) and `clientQuery` filters `items` directly.
  const [clientQuery, setClientQuery] = useState("");

  const loadSources = () => {
    getSkillSources()
      .then((r) => {
        setSources(r ?? []);
        // Keep selId valid; default to the first enabled source.
        const stillThere = (r ?? []).find((s) => s.id === selId && s.enabled);
        if (!stillThere) {
          const first = (r ?? []).find((s) => s.enabled);
          setSelId(first ? first.id : "");
        }
      })
      .catch(() => setSources([]));
  };

  useEffect(() => {
    loadSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load the catalog when the selected source, page, search, or category changes.
  useEffect(() => {
    if (!selId) {
      setItems([]);
      setCategories([]);
      setTotalCount(0);
      return;
    }
    setLoading(true);
    setErr("");
    const opts = isServerSearch
      ? { page, page_size: pageSize, query: searchQuery }
      : undefined;
    getSkillCatalog(selId, opts)
      .then((r) => {
        if (r.ok) {
          setItems(r.skills ?? []);
          setTotalCount(r.total_count ?? 0);
          setCategories(r.categories ?? []);
        } else {
          // Catalog fetch failed (git clone error, API down, etc.) — surface the error
          // instead of showing a silently-empty list (the original empty-catalog bug).
          setErr(r.error || "failed to load");
          setItems([]);
          setTotalCount(0);
          setCategories([]);
        }
      })
      .catch(() => setErr("failed to load"))
      .finally(() => setLoading(false));
  }, [selId, page, searchQuery, selCat]);

  // Reset to page 1 when the source or search query changes. Also clear the client-side
  // search input when switching sources so a stale filter doesn't carry over.
  useEffect(() => {
    setPage(1);
  }, [selId, searchQuery]);
  useEffect(() => {
    setSearchInput("");
    setClientQuery("");
  }, [selId]);

  const install = async (name: string, sourceId: string) => {
    setInstalling(name);
    setErr("");
    const r = await installSkill(sourceId, name);
    setInstalling("");
    if (r.ok) {
      setJustInstalled(name);
      // Refresh the catalog so the installed flag flips on this item.
      const opts = isServerSearch ? { page, page_size: pageSize, query: searchQuery } : undefined;
      const cat = await getSkillCatalog(sourceId, opts);
      if (cat.ok) setItems(cat.skills ?? []);
      // Close the marketplace shortly after a successful install so the user sees the
      // newly-installed skill in the Customize list. A short delay lets the "installed"
      // chip flash first.
      setTimeout(onInstalled, 500);
    } else {
      setErr(r.error || "install failed");
    }
  };

  const addSource = async () => {
    if (!addName.trim() || !addUrl.trim()) return;
    setAddBusy(true);
    const r = await addSkillSource(addName.trim(), addUrl.trim(), addType);
    setAddBusy(false);
    if (r.ok) {
      setAddName("");
      setAddUrl("");
      setAddType("http");
      setShowAdd(false);
      loadSources();
    } else {
      setErr(r.error || "failed to add source");
    }
  };

  const toggleSource = async (s: SkillSource) => {
    await updateSkillSource(s.id, { enabled: !s.enabled });
    loadSources();
  };

  const deleteSource = async (s: SkillSource) => {
    await removeSkillSource(s.id);
    loadSources();
  };

  const resetSources = async () => {
    await resetSkillSources();
    loadSources();
  };

  const doSearch = () => {
    const q = searchInput.trim();
    if (isServerSearch) {
      // Server-side search: commit the query, which triggers the catalog refetch effect.
      setSearchQuery(q);
    } else {
      // Client-side search: filter the already-loaded items (git/local/http catalogs are
      // small flat lists with no server-side query support).
      setClientQuery(q);
    }
  };

  if (sources.length === 0 && !showAdd) {
    return (
      <div className="py-6 text-center">
        <div className="text-[12.5px] text-faint mb-3">{t("customize.marketplace_empty")}</div>
        <div className="flex items-center justify-center gap-3">
          <button
            className="text-[12px] px-2.5 py-1 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent"
            onClick={() => setShowAdd(true)}
          >
            + {t("customize.marketplace_add_source")}
          </button>
          <button
            className="flex items-center gap-1 text-[12px] text-muted hover:text-accent"
            onClick={resetSources}
            title={t("customize.reset_sources_help")}
          >
            <Icon name="refresh" size={12} />
            {t("customize.reset_sources")}
          </button>
        </div>
      </div>
    );
  }

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  // Filter by category chip (client-side; the API doesn't support category filtering for
  // skills, so we filter the current page's items). For client-search sources we also
  // apply the `clientQuery` text filter here (name/description substring, case-insensitive).
  const q = clientQuery.toLowerCase();
  const visible = items.filter((it) => {
    if (selCat !== "__all" && it.category !== selCat) return false;
    if (q && !isServerSearch) {
      const hay = (it.name + " " + (it.description || "")).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  // Count label: server-search sources show the server-reported total; client-search
  // sources show the full catalog length (not the filtered count, so the user sees how
  // many skills the source has in total).
  const countTotal = isServerSearch ? totalCount : items.length;

  return (
    <div>
      {/* Source picker + manage */}
      <div className="mb-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {sources.filter((s) => s.enabled).map((s) => (
            <button
              key={s.id}
              className={
                "text-[12px] px-2.5 py-1 rounded-full border transition-colors " +
                (selId === s.id
                  ? "bg-accent text-white border-accent"
                  : "bg-paper border-line text-muted hover:text-ink")
              }
              onClick={() => setSelId(s.id)}
              title={s.url}
            >
              {s.name}
              {s.is_default && <span className="opacity-70"> · {t("customize.marketplace_source_default")}</span>}
            </button>
          ))}
          {sources.filter((s) => !s.enabled).length > 0 && (
            <span className="text-[11px] text-faint">
              +{sources.filter((s) => !s.enabled).length} disabled
            </span>
          )}
          <button
            className="text-[11.5px] px-2 py-1 rounded-full border border-line border-dashed text-muted hover:text-accent hover:border-accent"
            onClick={() => setShowAdd((v) => !v)}
          >
            + {t("customize.marketplace_add_source")}
          </button>
          <button
            className="flex items-center gap-1 text-[11.5px] px-2 py-1 rounded-full border border-line text-muted hover:text-accent hover:border-accent"
            onClick={resetSources}
            title={t("customize.reset_sources_help")}
          >
            <Icon name="refresh" size={12} />
            {t("customize.reset_sources")}
          </button>
        </div>

        {/* Source management (toggle + delete). All sources are deletable, including builtins. */}
        {sources.length > 0 && (
          <div className="mt-2 space-y-1">
            {sources.map((s) => (
              <div key={s.id} className="flex items-center gap-2 text-[11.5px] text-faint">
                <span className={"w-1.5 h-1.5 rounded-full " + (s.enabled ? "bg-accent" : "bg-faint")} />
                <span className="truncate flex-1 min-w-0" title={s.url}>{s.url}</span>
                <span className="text-[10px] px-1 rounded bg-paper border border-line shrink-0">{s.source_type}</span>
                <button
                  className="text-muted hover:text-accent"
                  onClick={() => toggleSource(s)}
                  title={s.enabled ? "Disable" : "Enable"}
                >
                  {s.enabled ? "●" : "○"}
                </button>
                <button
                  className="text-faint hover:text-danger p-0.5"
                  title={t("common.delete_aria", { title: s.name })}
                  onClick={() => deleteSource(s)}
                >
                  <Icon name="trash" size={12} />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Add-source form */}
        {showAdd && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 p-2 rounded-lg border border-line bg-paper">
            <input
              className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-1"
              placeholder={t("customize.marketplace_source_name_ph")}
              value={addName}
              spellCheck={false}
              onChange={(e) => setAddName(e.target.value)}
            />
            <input
              className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-[2]"
              placeholder={t("customize.marketplace_source_url_ph")}
              value={addUrl}
              spellCheck={false}
              onChange={(e) => setAddUrl(e.target.value)}
            />
            <select
              className="px-2 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent"
              value={addType}
              onChange={(e) => setAddType(e.target.value)}
            >
              <option value="http">http</option>
              <option value="git">git</option>
              <option value="local">local</option>
              <option value="modelscope">modelscope</option>
            </select>
            <button
              className="text-[12px] px-2.5 py-1 rounded-md bg-accent text-white hover:opacity-90 disabled:opacity-50"
              disabled={addBusy || !addName.trim() || !addUrl.trim()}
              onClick={addSource}
            >
              {t("customize.marketplace_add_source")}
            </button>
          </div>
        )}
      </div>

      {/* Search + total count — shown for all sources. modelscope/skillhub do server-side
          search (the query refetches the catalog); git/local/http filter client-side. */}
      <div className="flex items-center gap-2 mb-3">
        <input
          className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent flex-1 min-w-0"
          placeholder={t("customize.skill_search_placeholder")}
          value={searchInput}
          spellCheck={false}
          onChange={(e) => setSearchInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && doSearch()}
        />
        <button
          className="text-[12px] px-2.5 py-1 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0"
          onClick={doSearch}
        >
          {t("customize.marketplace_search")}
        </button>
        {countTotal > 0 && (
          <span className="text-[11px] text-faint shrink-0">
            {t("customize.skill_total_count", { count: countTotal })}
          </span>
        )}
      </div>

      {/* Category filter chips */}
      {categories.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-3">
          <button
            className={
              "text-[11.5px] px-2 py-0.5 rounded-full border transition-colors " +
              (selCat === "__all"
                ? "bg-accent text-white border-accent"
                : "bg-paper border-line text-muted hover:text-ink")
            }
            onClick={() => setSelCat("__all")}
          >
            {t("customize.marketplace_category_all")}
          </button>
          {categories.map((c) => (
            <button
              key={c}
              className={
                "text-[11.5px] px-2 py-0.5 rounded-full border transition-colors " +
                (selCat === c
                  ? "bg-accent text-white border-accent"
                  : "bg-paper border-line text-muted hover:text-ink")
              }
              onClick={() => setSelCat(c)}
            >
              {c}
            </button>
          ))}
        </div>
      )}

      {err && (
        <div className="text-[12px] text-danger py-2">{err}</div>
      )}

      {loading ? (
        <div className="text-[12.5px] text-faint py-6 text-center">
          {t("customize.marketplace_loading")}
        </div>
      ) : visible.length === 0 ? (
        <div className="text-[12.5px] text-faint py-6 text-center">
          {t("customize.marketplace_empty")}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {visible.map((it) => {
            const isInstalling = installing === it.name;
            const justDone = justInstalled === it.name;
            return (
              <div key={it.name} className="flex items-center gap-3 py-2.5">
                {it.icon ? (
                  <img
                    src={it.icon}
                    alt=""
                    className="w-7 h-7 rounded-md object-cover shrink-0 bg-paper border border-line"
                    onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                  />
                ) : (
                  <Icon name="file" size={14} className="text-faint shrink-0" />
                )}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-ink truncate">{it.name}</span>
                    {it.category && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-paper border border-line text-faint shrink-0">
                        {it.category}
                      </span>
                    )}
                    {it.author && (
                      <span className="text-[10.5px] text-faint truncate">· {it.author}</span>
                    )}
                    {it.downloads != null && it.downloads > 0 && (
                      <span className="text-[10px] text-faint shrink-0">↓ {it.downloads}</span>
                    )}
                  </div>
                  {it.description && (
                    <div className="text-[11.5px] text-faint truncate">{it.description}</div>
                  )}
                </div>
                {it.installed || justDone ? (
                  <span className="text-[10.5px] px-2 py-1 rounded-full bg-accentSoft text-accent shrink-0">
                    {t("customize.marketplace_installed")}
                  </span>
                ) : (
                  <button
                    className="text-[12px] px-2.5 py-1 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0 disabled:opacity-50"
                    disabled={isInstalling}
                    onClick={() => install(it.name, selId)}
                  >
                    {isInstalling ? t("customize.marketplace_installing") : t("customize.marketplace_install")}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Pagination (server-search sources only — git/local/http return one full page). */}
      {isServerSearch && totalCount > pageSize && (
        <div className="flex items-center justify-center gap-3 mt-3 pt-3 border-t border-line">
          <button
            className="text-[12px] px-2.5 py-1 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent disabled:opacity-40"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            ←
          </button>
          <span className="text-[11.5px] text-faint">
            {page} / {totalPages}
          </span>
          <button
            className="text-[12px] px-2.5 py-1 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent disabled:opacity-40"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            →
          </button>
        </div>
      )}
    </div>
  );
}

// Plugins tab (E4): pick a plugin source → filter by category → browse the marketplace.json
// catalog → install. Sources are fully manageable here (add/toggle/delete, mirroring
// DhpSourcesSection); the builtin "claude-official" source can't be deleted, only disabled.
// Install copies the plugin tree into state_dir()/plugins/<name>/ and registers any MCP
// servers declared in its plugin.json.
function PluginBrowseSection({ onInstalled }: { onInstalled: () => void }) {
  const { t } = useT();
  const [sources, setSources] = useState<PluginSource[]>([]);
  const [selId, setSelId] = useState<string>("");
  const [items, setItems] = useState<PluginCatalogItem[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [selCat, setSelCat] = useState<string>("__all");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [installing, setInstalling] = useState<string>("");
  const [justInstalled, setJustInstalled] = useState<string>("");

  // Inline source-add form state (Codex-style 3-field: url + ref + sparse_path, plus name).
  const [showAdd, setShowAdd] = useState(false);
  const [addName, setAddName] = useState("");
  const [addUrl, setAddUrl] = useState("");
  const [addRef, setAddRef] = useState("");
  const [addSparse, setAddSparse] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  // Client-side search (plugin catalogs are small — 276 items from claude-official — so we
  // filter in-browser rather than round-tripping a server query like the MCP/Skill tabs do).
  const [pluginQuery, setPluginQuery] = useState("");

  const loadSources = () => {
    getPluginSources()
      .then((r) => {
        setSources(r ?? []);
        // Keep selId valid; default to the first enabled source.
        const stillThere = (r ?? []).find((s) => s.id === selId && s.enabled);
        if (!stillThere) {
          const first = (r ?? []).find((s) => s.enabled);
          setSelId(first ? first.id : "");
        }
      })
      .catch(() => setSources([]));
  };

  useEffect(() => {
    loadSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load the catalog when the selected source changes.
  useEffect(() => {
    if (!selId) {
      setItems([]);
      setCategories([]);
      return;
    }
    setLoading(true);
    setErr("");
    getPluginCatalog(selId)
      .then((r) => {
        if (r.ok) {
          setItems(r.plugins ?? []);
          setCategories(r.categories ?? []);
          setSelCat("__all");
        } else {
          setErr(r.error || "failed to load");
          setItems([]);
          setCategories([]);
        }
      })
      .catch(() => setErr("failed to load"))
      .finally(() => setLoading(false));
  }, [selId]);

  const install = async (name: string, sourceId: string) => {
    setInstalling(name);
    setErr("");
    const r = await installPlugin(sourceId, name);
    setInstalling("");
    if (r.ok) {
      setJustInstalled(name);
      // Refresh the catalog so the installed flag flips on this item.
      const cat = await getPluginCatalog(sourceId);
      if (cat.ok) setItems(cat.plugins ?? []);
      // Close the marketplace shortly after a successful install so the user sees the
      // newly-installed plugin in the Customize ▸ Plugins list.
      setTimeout(onInstalled, 500);
    } else {
      setErr(r.error || "install failed");
    }
  };

  const addSource = async () => {
    if (!addName.trim() || !addUrl.trim()) return;
    setAddBusy(true);
    const r = await addPluginSource(
      addName.trim(), addUrl.trim(), "git", addRef.trim(), addSparse.trim(),
    );
    setAddBusy(false);
    if (r.ok) {
      setAddName("");
      setAddUrl("");
      setAddRef("");
      setAddSparse("");
      setShowAdd(false);
      loadSources();
    } else {
      setErr(r.error || "failed to add source");
    }
  };

  const toggleSource = async (s: PluginSource) => {
    await updatePluginSource(s.id, { enabled: !s.enabled });
    loadSources();
  };

  const deleteSource = async (s: PluginSource) => {
    // Builtins can't be deleted (backend rejects); only disabled.
    await removePluginSource(s.id);
    loadSources();
  };

  const resetSources = async () => {
    await resetPluginSources();
    loadSources();
  };

  // Filter by selected category chip + client-side search query.
  const q = pluginQuery.trim().toLowerCase();
  const visible = items.filter((it) => {
    if (selCat !== "__all" && it.category !== selCat) return false;
    if (!q) return true;
    return (
      it.name.toLowerCase().includes(q) ||
      (it.description || "").toLowerCase().includes(q) ||
      (it.author || "").toLowerCase().includes(q) ||
      (it.category || "").toLowerCase().includes(q)
    );
  });

  return (
    <div>
      {/* Source picker + manage */}
      <div className="mb-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {sources.filter((s) => s.enabled).map((s) => (
            <button
              key={s.id}
              className={
                "text-[12px] px-2.5 py-1 rounded-full border transition-colors " +
                (selId === s.id
                  ? "bg-accent text-white border-accent"
                  : "bg-paper border-line text-muted hover:text-ink")
              }
              onClick={() => setSelId(s.id)}
              title={s.url}
            >
              {s.name}
              {s.is_default && <span className="opacity-70"> · {t("customize.marketplace_source_default")}</span>}
            </button>
          ))}
          {sources.filter((s) => !s.enabled).length > 0 && (
            <span className="text-[11px] text-faint">
              +{sources.filter((s) => !s.enabled).length} disabled
            </span>
          )}
          <button
            className="text-[11.5px] px-2 py-1 rounded-full border border-line border-dashed text-muted hover:text-accent hover:border-accent"
            onClick={() => setShowAdd((v) => !v)}
          >
            + {t("customize.marketplace_add_source")}
          </button>
          <button
            className="flex items-center gap-1 text-[11.5px] px-2 py-1 rounded-full border border-line text-muted hover:text-accent hover:border-accent"
            onClick={resetSources}
            title={t("customize.reset_sources_help")}
          >
            <Icon name="refresh" size={12} />
            {t("customize.reset_sources")}
          </button>
        </div>

        {/* Source management (toggle + delete) for non-builtin sources, and a toggle for builtins. */}
        {sources.length > 0 && (
          <div className="mt-2 space-y-1">
            {sources.map((s) => (
              <div key={s.id} className="flex items-center gap-2 text-[11.5px] text-faint">
                <span className={"w-1.5 h-1.5 rounded-full " + (s.enabled ? "bg-accent" : "bg-faint")} />
                <span className="truncate flex-1 min-w-0" title={s.url}>{s.url}</span>
                <button
                  className="text-muted hover:text-accent"
                  onClick={() => toggleSource(s)}
                  title={s.enabled ? "Disable" : "Enable"}
                >
                  {s.enabled ? "●" : "○"}
                </button>
                <button
                  className="text-faint hover:text-danger p-0.5"
                  title={t("common.delete_aria", { title: s.name })}
                  onClick={() => deleteSource(s)}
                >
                  <Icon name="trash" size={12} />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Add-source form (Codex-style 3 fields: url + ref + sparse_path, plus name) */}
        {showAdd && (
          <div className="mt-2 p-2.5 rounded-lg border border-line bg-paper space-y-1.5">
            <div className="flex flex-wrap items-center gap-1.5">
              <input
                className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-1"
                placeholder={t("customize.marketplace_source_name_ph")}
                value={addName}
                spellCheck={false}
                onChange={(e) => setAddName(e.target.value)}
              />
              <input
                className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-[2]"
                placeholder={t("customize.plugin_source_url_ph")}
                value={addUrl}
                spellCheck={false}
                onChange={(e) => setAddUrl(e.target.value)}
              />
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <input
                className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-1"
                placeholder={t("customize.plugin_source_ref_ph")}
                value={addRef}
                spellCheck={false}
                onChange={(e) => setAddRef(e.target.value)}
              />
              <input
                className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-1"
                placeholder={t("customize.plugin_source_sparse_ph")}
                value={addSparse}
                spellCheck={false}
                onChange={(e) => setAddSparse(e.target.value)}
              />
              <button
                className="text-[12px] px-2.5 py-1 rounded-md bg-accent text-white hover:opacity-90 disabled:opacity-50 shrink-0"
                disabled={addBusy || !addName.trim() || !addUrl.trim()}
                onClick={addSource}
              >
                {t("customize.marketplace_add_source")}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Search + total count (client-side filter — plugin catalogs are small) */}
      <div className="flex items-center gap-2 mb-3">
        <input
          className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent flex-1 min-w-0"
          placeholder={t("customize.plugin_search_placeholder")}
          value={pluginQuery}
          spellCheck={false}
          onChange={(e) => setPluginQuery(e.target.value)}
        />
        {items.length > 0 && (
          <span className="text-[11px] text-faint shrink-0">
            {q
              ? t("customize.plugin_filtered_count", { shown: visible.length, total: items.length })
              : t("customize.plugin_total_count", { count: items.length })}
          </span>
        )}
      </div>

      {/* Category filter chips */}
      {categories.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-3">
          <button
            className={
              "text-[11.5px] px-2 py-0.5 rounded-full border transition-colors " +
              (selCat === "__all"
                ? "bg-accent text-white border-accent"
                : "bg-paper border-line text-muted hover:text-ink")
            }
            onClick={() => setSelCat("__all")}
          >
            {t("customize.marketplace_category_all")}
          </button>
          {categories.map((c) => (
            <button
              key={c}
              className={
                "text-[11.5px] px-2 py-0.5 rounded-full border transition-colors " +
                (selCat === c
                  ? "bg-accent text-white border-accent"
                  : "bg-paper border-line text-muted hover:text-ink")
              }
              onClick={() => setSelCat(c)}
            >
              {c}
            </button>
          ))}
        </div>
      )}

      {err && <div className="text-[12px] text-danger py-2">{err}</div>}

      {loading ? (
        <div className="text-[12.5px] text-faint py-6 text-center">
          {t("customize.marketplace_loading")}
        </div>
      ) : visible.length === 0 ? (
        <div className="text-[12.5px] text-faint py-6 text-center">
          {t("customize.marketplace_empty")}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {visible.map((it) => {
            const isInstalling = installing === it.name;
            const justDone = justInstalled === it.name;
            return (
              <div key={it.name} className="flex items-center gap-3 py-2.5">
                <Icon name="puzzle" size={14} className="text-faint shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-ink">{it.name}</span>
                    {it.category && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-paper border border-line text-faint shrink-0">
                        {it.category}
                      </span>
                    )}
                    {it.author && (
                      <span className="text-[10.5px] text-faint truncate">· {it.author}</span>
                    )}
                  </div>
                  {it.description && (
                    <div className="text-[11.5px] text-faint truncate">{it.description}</div>
                  )}
                </div>
                {it.installed || justDone ? (
                  <span className="text-[10.5px] px-2 py-1 rounded-full bg-accentSoft text-accent shrink-0">
                    {t("customize.marketplace_installed")}
                  </span>
                ) : (
                  <button
                    className="text-[12px] px-2.5 py-1 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0 disabled:opacity-50"
                    disabled={isInstalling}
                    onClick={() => install(it.name, selId)}
                  >
                    {isInstalling ? t("customize.marketplace_installing") : t("customize.marketplace_install")}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// MCP tab: pick an MCP marketplace source → browse its catalog (paginated, searchable) →
// install. The built-in ModelScope MCP plaza (魔搭社区 MCP 广场) has ~9.8k servers served via
// the public agg API. Each server carries a ServerConfig in the standard Claude Code format
// ({mcpServers: {name: {command, args}}}) — install extracts it and registers via add_mcp.
// Sources are fully manageable here (add/toggle/delete, mirroring SkillBrowseSection).
function McpBrowseSection({ onInstalled }: { onInstalled: () => void }) {
  const { t } = useT();
  const [sources, setSources] = useState<McpSource[]>([]);
  const [selId, setSelId] = useState<string>("");
  const [items, setItems] = useState<McpCatalogItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [installing, setInstalling] = useState<string>("");
  const [justInstalled, setJustInstalled] = useState<string>("");
  const [page, setPage] = useState(1);
  // The ModelScope agg API ignores PageSize (always returns max 10) and PageNumber (page 2
  // returns the same 10 as page 1), so pageSize is set to 10 to match reality and pagination
  // is hidden entirely — there's no next page to fetch. Users narrow via search/category.
  const [pageSize] = useState(10);
  const [totalCount, setTotalCount] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchInput, setSearchInput] = useState("");
  // Category chips are rendered from the hardcoded MCP_CATEGORIES list (the agg API has no
  // category endpoint and its Criterion filter is ignored). Selecting a category folds the
  // slug into the agg `Query` server-side, so `items` is already category-scoped.
  const [selCat, setSelCat] = useState<string>("__all");

  const [showAdd, setShowAdd] = useState(false);
  const [addName, setAddName] = useState("");
  const [addUrl, setAddUrl] = useState("");
  const [addBusy, setAddBusy] = useState(false);

  const loadSources = () => {
    getMcpSources()
      .then((r) => {
        setSources(r ?? []);
        const stillThere = (r ?? []).find((s) => s.id === selId && s.enabled);
        if (!stillThere) {
          const first = (r ?? []).find((s) => s.enabled);
          setSelId(first ? first.id : "");
        }
      })
      .catch(() => setSources([]));
  };

  useEffect(() => {
    loadSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selId) {
      setItems([]);
      setTotalCount(0);
      return;
    }
    setLoading(true);
    setErr("");
    getMcpCatalog(selId, { page, page_size: pageSize, query: searchQuery, category: selCat === "__all" ? "" : selCat })
      .then((r) => {
        if (r.ok) {
          setItems(r.servers ?? []);
          setTotalCount(r.total_count ?? 0);
        } else {
          setErr(r.error || "failed to load");
          setItems([]);
          setTotalCount(0);
        }
      })
      .catch(() => setErr("failed to load"))
      .finally(() => setLoading(false));
  }, [selId, page, searchQuery, selCat]);

  useEffect(() => {
    setPage(1);
  }, [selId, searchQuery, selCat]);

  const install = async (name: string, sourceId: string) => {
    setInstalling(name);
    setErr("");
    const r = await installMcpFromCatalog(sourceId, name);
    setInstalling("");
    if (r.ok) {
      setJustInstalled(name);
      const cat = await getMcpCatalog(sourceId, { page, page_size: pageSize, query: searchQuery, category: selCat === "__all" ? "" : selCat });
      if (cat.ok) setItems(cat.servers ?? []);
      setTimeout(onInstalled, 500);
    } else {
      setErr(r.error || "install failed");
    }
  };

  const addSource = async () => {
    if (!addName.trim() || !addUrl.trim()) return;
    setAddBusy(true);
    const r = await addMcpSource(addName.trim(), addUrl.trim(), "modelscope");
    setAddBusy(false);
    if (r.ok) {
      setAddName("");
      setAddUrl("");
      setShowAdd(false);
      loadSources();
    } else {
      setErr(r.error || "failed to add source");
    }
  };

  const toggleSource = async (s: McpSource) => {
    await updateMcpSource(s.id, { enabled: !s.enabled });
    loadSources();
  };

  const deleteSource = async (s: McpSource) => {
    await removeMcpSource(s.id);
    loadSources();
  };

  const resetSources = async () => {
    await resetMcpSources();
    loadSources();
  };

  const doSearch = () => {
    setSearchQuery(searchInput.trim());
  };

  if (sources.length === 0 && !showAdd) {
    return (
      <div className="py-6 text-center">
        <div className="text-[12.5px] text-faint mb-3">{t("customize.marketplace_empty")}</div>
        <div className="flex items-center justify-center gap-3">
          <button
            className="text-[12px] px-2.5 py-1 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent"
            onClick={() => setShowAdd(true)}
          >
            + {t("customize.marketplace_add_source")}
          </button>
          <button
            className="flex items-center gap-1 text-[12px] text-muted hover:text-accent"
            onClick={resetSources}
            title={t("customize.reset_sources_help")}
          >
            <Icon name="refresh" size={12} />
            {t("customize.reset_sources")}
          </button>
        </div>
      </div>
    );
  }

  // No client-side category filter: the backend folds the selected category into the agg
  // API `Query` (the only field it honors — Criterion/PageSize/PageNumber are all ignored),
  // so `items` is already scoped to the selected category. Re-filtering here would drop
  // servers whose `category` list doesn't literally contain the slug even though they
  // matched the query, showing 0 results for a non-empty category.
  const visible = items;

  return (
    <div>
      {/* Source picker + manage */}
      <div className="mb-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {sources.filter((s) => s.enabled).map((s) => (
            <button
              key={s.id}
              className={
                "text-[12px] px-2.5 py-1 rounded-full border transition-colors " +
                (selId === s.id
                  ? "bg-accent text-white border-accent"
                  : "bg-paper border-line text-muted hover:text-ink")
              }
              onClick={() => setSelId(s.id)}
              title={s.url}
            >
              {s.name}
              {s.is_default && <span className="opacity-70"> · {t("customize.marketplace_source_default")}</span>}
            </button>
          ))}
          {sources.filter((s) => !s.enabled).length > 0 && (
            <span className="text-[11px] text-faint">
              +{sources.filter((s) => !s.enabled).length} disabled
            </span>
          )}
          <button
            className="text-[11.5px] px-2 py-1 rounded-full border border-line border-dashed text-muted hover:text-accent hover:border-accent"
            onClick={() => setShowAdd((v) => !v)}
          >
            + {t("customize.marketplace_add_source")}
          </button>
          <button
            className="flex items-center gap-1 text-[11.5px] px-2 py-1 rounded-full border border-line text-muted hover:text-accent hover:border-accent"
            onClick={resetSources}
            title={t("customize.reset_sources_help")}
          >
            <Icon name="refresh" size={12} />
            {t("customize.reset_sources")}
          </button>
        </div>

        {sources.length > 0 && (
          <div className="mt-2 space-y-1">
            {sources.map((s) => (
              <div key={s.id} className="flex items-center gap-2 text-[11.5px] text-faint">
                <span className={"w-1.5 h-1.5 rounded-full " + (s.enabled ? "bg-accent" : "bg-faint")} />
                <span className="truncate flex-1 min-w-0" title={s.url}>{s.url}</span>
                <span className="text-[10px] px-1 rounded bg-paper border border-line shrink-0">{s.source_type}</span>
                <button
                  className="text-muted hover:text-accent"
                  onClick={() => toggleSource(s)}
                  title={s.enabled ? "Disable" : "Enable"}
                >
                  {s.enabled ? "●" : "○"}
                </button>
                <button
                  className="text-faint hover:text-danger p-0.5"
                  title={t("common.delete_aria", { title: s.name })}
                  onClick={() => deleteSource(s)}
                >
                  <Icon name="trash" size={12} />
                </button>
              </div>
            ))}
          </div>
        )}

        {showAdd && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 p-2 rounded-lg border border-line bg-paper">
            <input
              className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-1"
              placeholder={t("customize.marketplace_source_name_ph")}
              value={addName}
              spellCheck={false}
              onChange={(e) => setAddName(e.target.value)}
            />
            <input
              className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-[2]"
              placeholder={t("customize.marketplace_source_url_ph")}
              value={addUrl}
              spellCheck={false}
              onChange={(e) => setAddUrl(e.target.value)}
            />
            <button
              className="text-[12px] px-2.5 py-1 rounded-md bg-accent text-white hover:opacity-90 disabled:opacity-50"
              disabled={addBusy || !addName.trim() || !addUrl.trim()}
              onClick={addSource}
            >
              {t("customize.marketplace_add_source")}
            </button>
          </div>
        )}
      </div>

      {/* Search + total count */}
      <div className="flex items-center gap-2 mb-3">
        <input
          className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent flex-1 min-w-0"
          placeholder={t("customize.mcp_search_placeholder")}
          value={searchInput}
          spellCheck={false}
          onChange={(e) => setSearchInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && doSearch()}
        />
        <button
          className="text-[12px] px-2.5 py-1 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0"
          onClick={doSearch}
        >
          {t("customize.marketplace_search")}
        </button>
        {totalCount > 0 && (
          <span className="text-[11px] text-faint shrink-0">
            {t("customize.mcp_total_count", { count: totalCount })}
          </span>
        )}
      </div>

      {/* Category filter chips — always render the full 13 hardcoded ModelScope MCP
          categories with Chinese labels. Selecting a chip folds the slug into the agg API
          `Query` (the only field it honors — Criterion/PageSize/PageNumber are all ignored),
          so the backend returns servers matching that category with an honest total. No
          per-category counts are shown: the API can't surface the site's "589" figure, so
          showing it would mislead. */}
      <div className="flex flex-wrap gap-1 mb-3">
        <button
          className={
            "text-[11.5px] px-2 py-0.5 rounded-full border transition-colors " +
            (selCat === "__all"
              ? "bg-accent text-white border-accent"
              : "bg-paper border-line text-muted hover:text-ink")
          }
          onClick={() => setSelCat("__all")}
        >
          {t("customize.marketplace_category_all")}
        </button>
        {MCP_CATEGORIES.map((c) => (
          <button
            key={c.slug}
            className={
              "text-[11.5px] px-2 py-0.5 rounded-full border transition-colors " +
              (selCat === c.slug
                ? "bg-accent text-white border-accent"
                : "bg-paper border-line text-muted hover:text-ink")
            }
            onClick={() => setSelCat(c.slug)}
          >
            {c.zh}
          </button>
        ))}
      </div>

      {err && <div className="text-[12px] text-danger py-2">{err}</div>}

      {loading ? (
        <div className="text-[12.5px] text-faint py-6 text-center">
          {t("customize.marketplace_loading")}
        </div>
      ) : items.length === 0 ? (
        <div className="text-[12.5px] text-faint py-6 text-center">
          {t("customize.marketplace_empty")}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {visible.map((it) => {
            const isInstalling = installing === it.name;
            const justDone = justInstalled === it.name;
            return (
              <div key={it.name} className="flex items-center gap-3 py-2.5">
                {it.icon ? (
                  <img
                    src={it.icon}
                    alt=""
                    className="w-7 h-7 rounded-md object-cover shrink-0 bg-paper border border-line"
                    onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                  />
                ) : (
                  <Icon name="code" size={14} className="text-faint shrink-0" />
                )}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-medium text-ink truncate">{it.name}</span>
                    {it.verified && (
                      <span className="text-[9.5px] px-1.5 py-0.5 rounded-full bg-accentSoft text-accent shrink-0" title={t("customize.mcp_verified")}>
                        ✓ {t("customize.mcp_verified")}
                      </span>
                    )}
                    {it.hosted && (
                      <span className="text-[9.5px] px-1.5 py-0.5 rounded-full bg-paper border border-line text-faint shrink-0">
                        {t("customize.mcp_hosted")}
                      </span>
                    )}
                    {it.category.map((c) => (
                      <span key={c} className="text-[10px] px-1.5 py-0.5 rounded-full bg-paper border border-line text-faint shrink-0">
                        {mcpCategoryLabel(c)}
                      </span>
                    ))}
                    {it.stars > 0 && (
                      <span className="text-[10px] text-faint shrink-0">★ {it.stars}</span>
                    )}
                  </div>
                  {it.description && (
                    <div className="text-[11.5px] text-faint truncate">{it.description}</div>
                  )}
                </div>
                {it.installed || justDone ? (
                  <span className="text-[10.5px] px-2 py-1 rounded-full bg-accentSoft text-accent shrink-0">
                    {t("customize.marketplace_installed")}
                  </span>
                ) : (
                  <button
                    className="text-[12px] px-2.5 py-1 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0 disabled:opacity-50"
                    disabled={isInstalling}
                    onClick={() => install(it.name, selId)}
                  >
                    {isInstalling ? t("customize.marketplace_installing") : t("customize.marketplace_install")}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* The ModelScope agg API ignores PageNumber (page 2 returns the same 10 as page 1)
          and caps results at 10 per call, so real pagination is impossible. Instead of a
          broken "1 / 328" pager that always shows the same items, show an honest note when
          there are more results than the API surfaced, pointing the user to search/category. */}
      {totalCount > items.length && (
        <div className="text-[11px] text-faint text-center mt-3 pt-3 border-t border-line">
          {t("customize.mcp_limited_results", { shown: items.length, total: totalCount })}
        </div>
      )}
    </div>
  );
}

// Subagents tab (E4 后续): persona marketplace. A persona source is a git repo of
// *.md persona manifests. Pick a source → browse its catalog → install one persona.
// Install reuses the consent path (lands disabled pending approval); the marketplace
// never changes the trust model. No builtin source today — the user adds their own.
function PersonaBrowseSection({ onInstalled }: { onInstalled: () => void }) {
  const { t } = useT();
  const [sources, setSources] = useState<PersonaSource[]>([]);
  const [selId, setSelId] = useState<string>("");
  const [items, setItems] = useState<PersonaCatalogItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [installing, setInstalling] = useState<string>("");
  const [justInstalled, setJustInstalled] = useState<string>("");

  // Inline source-add form state.
  const [showAdd, setShowAdd] = useState(false);
  const [addName, setAddName] = useState("");
  const [addUrl, setAddUrl] = useState("");
  const [addBusy, setAddBusy] = useState(false);

  const loadSources = () => {
    getPersonaSources()
      .then((r) => {
        setSources(r ?? []);
        const stillThere = (r ?? []).find((s) => s.id === selId && s.enabled);
        if (!stillThere) {
          const first = (r ?? []).find((s) => s.enabled);
          setSelId(first ? first.id : "");
        }
      })
      .catch(() => setSources([]));
  };

  useEffect(() => {
    loadSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load the catalog when the selected source changes.
  useEffect(() => {
    if (!selId) {
      setItems([]);
      return;
    }
    setLoading(true);
    setErr("");
    getPersonaCatalog(selId)
      .then((r) => {
        if (r.ok) {
          setItems(r.personas ?? []);
        } else {
          setErr(r.error || "failed to load");
          setItems([]);
        }
      })
      .catch(() => setErr("failed to load"))
      .finally(() => setLoading(false));
  }, [selId]);

  const install = async (personaId: string, sourceId: string) => {
    setInstalling(personaId);
    setErr("");
    const r = await installPersonaFromSource(sourceId, personaId);
    setInstalling("");
    if (r.ok) {
      setJustInstalled(personaId);
      // Refresh the catalog so the installed flag flips on this item.
      const cat = await getPersonaCatalog(sourceId);
      if (cat.ok) setItems(cat.personas ?? []);
      // Close the marketplace so the user sees the newly-installed persona
      // (disabled pending consent) in Settings ▸ Personas.
      setTimeout(onInstalled, 500);
    } else {
      setErr(r.error || "install failed");
    }
  };

  const addSource = async () => {
    if (!addName.trim() || !addUrl.trim()) return;
    setAddBusy(true);
    const r = await addPersonaSource(addName.trim(), addUrl.trim());
    setAddBusy(false);
    if (r.ok) {
      setAddName("");
      setAddUrl("");
      setShowAdd(false);
      loadSources();
    } else {
      setErr(r.error || "failed to add source");
    }
  };

  const toggleSource = async (s: PersonaSource) => {
    await updatePersonaSource(s.id, { enabled: !s.enabled });
    loadSources();
  };

  const deleteSource = async (s: PersonaSource) => {
    await removePersonaSource(s.id);
    loadSources();
  };

  return (
    <div>
      {/* Source picker + manage */}
      <div className="mb-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {sources.filter((s) => s.enabled).map((s) => (
            <button
              key={s.id}
              className={
                "text-[12px] px-2.5 py-1 rounded-full border transition-colors " +
                (selId === s.id
                  ? "bg-accent text-white border-accent"
                  : "bg-paper border-line text-muted hover:text-ink")
              }
              onClick={() => setSelId(s.id)}
              title={s.url}
            >
              {s.name}
            </button>
          ))}
          {sources.filter((s) => !s.enabled).length > 0 && (
            <span className="text-[11px] text-faint">
              +{sources.filter((s) => !s.enabled).length} disabled
            </span>
          )}
          <button
            className="text-[11.5px] px-2 py-1 rounded-full border border-line border-dashed text-muted hover:text-accent hover:border-accent"
            onClick={() => setShowAdd((v) => !v)}
          >
            + {t("customize.marketplace_add_source")}
          </button>
        </div>

        {/* Source management (toggle + delete). No builtins today, so every source is deletable. */}
        {sources.length > 0 && (
          <div className="mt-2 space-y-1">
            {sources.map((s) => (
              <div key={s.id} className="flex items-center gap-2 text-[11.5px] text-faint">
                <span className={"w-1.5 h-1.5 rounded-full " + (s.enabled ? "bg-accent" : "bg-faint")} />
                <span className="truncate flex-1 min-w-0" title={s.url}>{s.url}</span>
                <button
                  className="text-muted hover:text-accent"
                  onClick={() => toggleSource(s)}
                  title={s.enabled ? "Disable" : "Enable"}
                >
                  {s.enabled ? "●" : "○"}
                </button>
                <button
                  className="text-faint hover:text-danger p-0.5"
                  title={t("common.delete_aria", { title: s.name })}
                  onClick={() => deleteSource(s)}
                >
                  <Icon name="trash" size={12} />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Add-source form */}
        {showAdd && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 p-2 rounded-lg border border-line bg-paper">
            <input
              className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-1"
              placeholder={t("customize.marketplace_source_name_ph")}
              value={addName}
              spellCheck={false}
              onChange={(e) => setAddName(e.target.value)}
            />
            <input
              className="px-2.5 py-1 rounded-md border border-line bg-panel text-[12px] text-ink outline-none focus:border-accent min-w-0 flex-[2]"
              placeholder={t("customize.marketplace_source_url_ph")}
              value={addUrl}
              spellCheck={false}
              onChange={(e) => setAddUrl(e.target.value)}
            />
            <button
              className="text-[12px] px-2.5 py-1 rounded-md bg-accent text-white hover:opacity-90 disabled:opacity-50"
              disabled={addBusy || !addName.trim() || !addUrl.trim()}
              onClick={addSource}
            >
              {t("customize.marketplace_add_source")}
            </button>
          </div>
        )}
      </div>

      {err && <div className="text-[12px] text-danger py-2">{err}</div>}

      {loading ? (
        <div className="text-[12.5px] text-faint py-6 text-center">
          {t("customize.marketplace_loading")}
        </div>
      ) : items.length === 0 ? (
        <div className="text-[12.5px] text-faint py-6 text-center">
          {selId ? t("customize.marketplace_empty") : t("customize.persona_marketplace_no_source")}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {items.map((it) => {
            const isInstalling = installing === it.id;
            const justDone = justInstalled === it.id;
            return (
              <div key={it.id} className="flex items-center gap-3 py-2.5">
                <Icon name="bot" size={14} className="text-faint shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-ink">{it.name}</span>
                    {it.family && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-paper border border-line text-faint shrink-0">
                        {it.family}
                      </span>
                    )}
                  </div>
                  {(it.tagline || it.description) && (
                    <div className="text-[11.5px] text-faint truncate">
                      {it.tagline || it.description}
                    </div>
                  )}
                </div>
                {it.installed || justDone ? (
                  <span className="text-[10.5px] px-2 py-1 rounded-full bg-accentSoft text-accent shrink-0">
                    {t("customize.marketplace_installed")}
                  </span>
                ) : (
                  <button
                    className="text-[12px] px-2.5 py-1 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0 disabled:opacity-50"
                    disabled={isInstalling}
                    onClick={() => install(it.id, selId)}
                  >
                    {isInstalling ? t("customize.marketplace_installing") : t("customize.marketplace_install")}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
