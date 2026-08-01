import type { SessionInfo, WsEvent } from "./types";

declare const __COWORKER_DEV_TOKEN__: string;

// Endpoint resolution order: runtime-injected globals (Tauri sets `window.__COWORKER_HTTP__`
// for its dynamically-chosen sidecar port) → Vite env → the 127.0.0.1:8765 dev default. This
// keeps a single codebase: browser `npm run dev` hits 8765; the desktop shell hits its sidecar.
const httpBase = (): string =>
  (globalThis as any).__COWORKER_HTTP__ ||
  (import.meta as any).env?.VITE_COWORKER_HTTP ||
  "http://127.0.0.1:8765";
const wsBase = (): string =>
  (globalThis as any).__COWORKER_WS__ ||
  (import.meta as any).env?.VITE_COWORKER_WS ||
  "ws://127.0.0.1:8765";
const apiToken = (): string =>
  (globalThis as any).__COWORKER_API_TOKEN__ ||
  (import.meta as any).env?.VITE_COWORKER_API_TOKEN ||
  (typeof __COWORKER_DEV_TOKEN__ === "string" ? __COWORKER_DEV_TOKEN__ : "");

// Dev-only token self-heal. The baked-in __COWORKER_DEV_TOKEN__ is frozen at vite startup,
// so a sidecar restart (which rotates sidecar-8765.token) silently breaks every request with
// 401 until vite is manually restarted. The vite dev server re-reads the token file on each
// request to /__dev_token__ (see vite.config.ts); on a 401 we fetch it once, cache the fresh
// token, and retry. No-op in the desktop shell (Tauri injects its in-memory token; the
// runtime global short-circuits apiToken() above so this path is never reached).
let devTokenOverride = "";
let devTokenFetching: Promise<string> | null = null;
const refreshDevToken = async (): Promise<string> => {
  if (devTokenOverride) return devTokenOverride;
  if (!devTokenFetching) {
    // The middleware lives on the vite dev server (window.location.origin, port 1420), NOT on
    // the sidecar (httpBase, port 8765) — the sidecar has no such route and would 401.
    const origin =
      (typeof window !== "undefined" && window.location && window.location.origin) ||
      httpBase();
    devTokenFetching = globalThis
      .fetch(`${origin}/__dev_token__.json`)
      .then((r) => r.json())
      .then((d: { token?: string }) => {
        const t = d.token || "";
        if (t) devTokenOverride = t; // cache for subsequent retries
        devTokenFetching = null;
        return t;
      })
      .catch(() => {
        devTokenFetching = null;
        return "";
      });
  }
  return devTokenFetching;
};

// All local REST calls pass through this module, so a module-local wrapper applies launch
// authentication without asking every endpoint helper to remember the security header. In dev
// (browser) mode, a 401 triggers one token self-heal + retry (sidecar restarts rotate the token).
const fetch = async (
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> => {
  const applyToken = (headers: Headers) => {
    const token = devTokenOverride || apiToken();
    if (token) headers.set("X-OpenWorker-Token", token);
    return headers;
  };
  const headers = applyToken(new Headers(init.headers));
  const res = await globalThis.fetch(input, { ...init, headers });
  if (res.status !== 401) return res;
  // 401: try to refresh the dev token once and retry. Skip in the desktop shell (Tauri's
  // in-memory token never rotates this way) and skip if already retried via init.
  if ((init as any).__retried) return res;
  const fresh = await refreshDevToken();
  if (!fresh) return res;
  const retryHeaders = applyToken(new Headers(init.headers));
  return globalThis.fetch(input, { ...init, headers: retryHeaders, __retried: true } as RequestInit);
};

const openWebSocket = (url: string): WebSocket => {
  // devTokenOverride is set by the REST self-heal after a sidecar restart; without it the
  // WebSocket would keep using the stale frozen __COWORKER_DEV_TOKEN__ and 403-loop until a
  // full page reload that re-runs the self-heal. Prefer the override, then fall back.
  const token = devTokenOverride || apiToken();
  return token
    ? new WebSocket(url, ["openworker", token])
    : new WebSocket(url);
};

export interface Health {
  status: string;
  default_workspace: string | null;
  model: string;
}

export interface RecentWorkspace {
  path: string;
  name: string;
  exists: boolean;
}

export interface WorkspaceCommandTrust {
  workspace: string;
  requested_commands: string[];
  trusted: boolean;
  required: boolean;
  exists?: boolean;
}

export async function getHealth(): Promise<Health> {
  const res = await fetch(`${httpBase()}/v1/health`);
  return res.json();
}

export async function getRecentWorkspaces(): Promise<RecentWorkspace[]> {
  const res = await fetch(`${httpBase()}/v1/workspaces/recent`);
  return (await res.json()).workspaces ?? [];
}

/** Ask the LOCAL sidecar to open the OS folder picker — the browser GUI can't obtain absolute
 * paths from web file dialogs. Blocks until the user picks or cancels; null on cancel/unavailable. */
export async function pickFolderViaServer(): Promise<string | null> {
  try {
    const res = await fetch(`${httpBase()}/v1/workspaces/pick`, { method: "POST" });
    const d = await res.json();
    return d.ok && d.path ? d.path : null;
  } catch {
    return null;
  }
}

export async function openWorkspace(
  path: string,
  create = false,
): Promise<{
  path: string;
  ok: boolean;
  error?: string;
  git_branch?: string | null;
  command_trust?: WorkspaceCommandTrust;
}> {
  const res = await fetch(`${httpBase()}/v1/workspaces/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, create }),
  });
  return res.json();
}

export async function getTrustedWorkspaces(): Promise<WorkspaceCommandTrust[]> {
  const res = await fetch(`${httpBase()}/v1/workspaces/trusted`);
  return (await res.json()).workspaces ?? [];
}

export async function setWorkspaceTrusted(
  path: string,
  trusted: boolean,
): Promise<{ ok: boolean; error?: string } & WorkspaceCommandTrust> {
  const res = await fetch(`${httpBase()}/v1/workspaces/trust`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, trusted }),
  });
  return res.json();
}

export async function getSessions(workspace?: string): Promise<SessionInfo[]> {
  const q = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
  const res = await fetch(`${httpBase()}/v1/sessions${q}`);
  return (await res.json()).sessions ?? [];
}

// A structured connector-delivered inbound message (§3.1). Attached to the user message it framed,
// for display only — the model still sees the framed `content`; this drives the ConnectorMessageCard.
export interface MessageSource {
  connector: string; // platform id, e.g. "slack"
  kind: "channel" | "dm";
  channel_id: string; // e.g. "C0BD7KZ1AH5"
  channel_name: string; // resolved; may equal the id (e.g. "#ocw-test")
  sender_id: string;
  sender_name: string; // resolved; may equal the id
  ts: number; // epoch seconds
  text: string; // the RAW message (what the card shows)
}

// A transcript message from GET /v1/sessions/{id}/messages. Kept permissive (open shape) because
// itemsFromMessages reads several role-specific fields; `source` is the optional connector sidecar.
export interface ConversationMessage {
  role: string;
  content?: any;
  tool_calls?: any[];
  tool_call_id?: string;
  source?: MessageSource;
  // Token counts for the round-trip that produced an assistant message
  // ({model, input, output, cache_read, cache_write}); absent on older servers.
  usage?: import("./types").TurnUsage;
  [key: string]: any;
}

export async function getSessionMessages(sessionId: string): Promise<ConversationMessage[]> {
  const res = await fetch(`${httpBase()}/v1/sessions/${sessionId}/messages`);
  return (await res.json()).messages ?? [];
}

export async function renameSession(sessionId: string, title: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  return res.json();
}

export async function setSessionFlags(
  sessionId: string,
  flags: { pinned?: boolean; archived?: boolean },
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(flags),
  });
  return res.json();
}

export async function deleteSession(sessionId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  return res.json();
}

export interface ArtifactInfo {
  path: string; // workspace-relative (the display/API identifier)
  abs_path?: string; // absolute — what "Copy path" copies
  name: string;
  kind: "markdown" | "html" | "image" | "code" | "text" | string;
  size: number;
  modified_at: number;
}

export interface ArtifactContent {
  ok: boolean;
  error?: string;
  path: string;
  kind: string;
  content?: string;
  data_url?: string;
  truncated?: boolean;
}

export async function getArtifacts(sessionId: string): Promise<ArtifactInfo[]> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/artifacts`);
  return (await res.json()).artifacts ?? [];
}

export async function readArtifact(sessionId: string, path: string): Promise<ArtifactContent> {
  const q = new URLSearchParams({ path });
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/artifacts/read?${q.toString()}`);
  return res.json();
}

/** Show the artifact in the OS file manager ("reveal") or open it with its default app ("open"). */
export async function revealArtifact(
  sessionId: string,
  path: string,
  mode: "reveal" | "open" = "reveal",
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/artifacts/reveal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, mode }),
  });
  return res.json();
}

// -- session roots (orphan Cowork: scratch + added folders) -------------------
export interface RootInfo {
  path: string;
  writable: boolean;
  label: string;
  primary: boolean;
  exists: boolean;
}

export async function getRoots(sessionId: string): Promise<RootInfo[]> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/roots`);
  return (await res.json()).roots ?? [];
}

export async function addRoot(
  sessionId: string,
  path: string,
  writable: boolean,
): Promise<{ ok: boolean; error?: string; roots?: RootInfo[] }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/roots`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, writable }),
  });
  return res.json();
}

export async function removeRoot(
  sessionId: string,
  path: string,
): Promise<{ ok: boolean; error?: string; roots?: RootInfo[] }> {
  const q = new URLSearchParams({ path });
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/roots?${q.toString()}`,
    { method: "DELETE" },
  );
  return res.json();
}

// -- MCP servers --------------------------------------------------------------
export interface McpServer {
  name: string;
  enabled: boolean;
  transport: string;
  requires_approval: boolean;
  // "connected" | "configured" | "disabled" | and for auth:"oauth" servers:
  // "needs_auth" (no tokens yet) | "authorizing" (browser sign-in in flight)
  status: string;
  auth?: "oauth" | null;
  last_error?: string | null;
  tool_count: number | null;
  config: Record<string, any>;
}

export async function getMcpServers(): Promise<McpServer[]> {
  const res = await fetch(`${httpBase()}/v1/mcp`);
  return (await res.json()).servers ?? [];
}

export async function addMcpServer(name: string, config: Record<string, any>) {
  const res = await fetch(`${httpBase()}/v1/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, config }),
  });
  return res.json();
}

export async function patchMcpServer(name: string, changes: Record<string, any>) {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function deleteMcpServer(name: string) {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}`, { method: "DELETE" });
  return res.json();
}

export async function getMcpTools(
  name: string,
): Promise<{ ok: boolean; error?: string; tools: { name: string; description: string }[] }> {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}/tools`);
  return res.json();
}

export async function reloadMcp() {
  const res = await fetch(`${httpBase()}/v1/mcp/reload`, { method: "POST" });
  return res.json();
}

/** Connect one MCP server now. For OAuth servers this opens the system browser;
 * poll getMcpServers() for the status flip (authorizing → connected / needs_auth). */
export async function connectMcp(name: string): Promise<{ ok: boolean; started?: boolean }> {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}/connect`, {
    method: "POST",
  });
  return res.json();
}

/** Drop the connection and forget the stored OAuth tokens. */
export async function signoutMcp(name: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}/signout`, {
    method: "POST",
  });
  return res.json();
}

// -- skills -------------------------------------------------------------------
// Skills follow the Anthropic SKILL.md format (progressive disclosure).
// The backend currently only exposes list_skills (no install/uninstall yet — E1).
export interface Skill {
  name: string;
  description: string;
}

export async function getSkills(): Promise<Skill[]> {
  const res = await fetch(`${httpBase()}/v1/skills`);
  return (await res.json()).skills ?? [];
}

// -- skill sources + install/uninstall (E1) -----------------------------------
// Mirrors the DHP source API: list/add/update/remove sources, browse a source's catalog,
// install a named skill into state_dir()/skills, uninstall by name.
export interface SkillSource {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  is_default: boolean;
  source_type: string; // "git" | "local" | "http"
}

export interface SkillCatalogItem {
  name: string;
  description: string;
  path: string;
  installed: boolean;
}

export async function getSkillSources(): Promise<SkillSource[]> {
  const res = await fetch(`${httpBase()}/v1/skills/sources`);
  return (await res.json()).sources ?? [];
}

export async function addSkillSource(
  name: string,
  url: string,
  sourceType = "http",
): Promise<{ ok: boolean; error?: string; source?: SkillSource }> {
  const res = await fetch(`${httpBase()}/v1/skills/sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, url, source_type: sourceType }),
  });
  return res.json();
}

export async function updateSkillSource(
  sourceId: string,
  changes: Partial<SkillSource>,
): Promise<{ ok: boolean; error?: string; source?: SkillSource }> {
  const res = await fetch(`${httpBase()}/v1/skills/sources/${sourceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function removeSkillSource(
  sourceId: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/skills/sources/${sourceId}`, {
    method: "DELETE",
  });
  return res.json();
}

export async function getSkillCatalog(
  sourceId: string,
): Promise<{ ok: boolean; error?: string; source?: SkillSource; skills?: SkillCatalogItem[] }> {
  const res = await fetch(`${httpBase()}/v1/skills/sources/${sourceId}/catalog`);
  return res.json();
}

export async function installSkill(
  sourceId: string,
  name: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/skills/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_id: sourceId, name }),
  });
  return res.json();
}

export async function uninstallSkill(name: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  return res.json();
}

// -- Plugins (marketplace install/uninstall, E4) ------------------------------
// Claude-Code-format plugins installed from marketplace sources. The official
// claude-plugins-official.git is built in. Plugins land under state_dir()/plugins/<name>/;
// their skills/ and commands/ subfolders are picked up by the existing loaders.
export interface PluginSource {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  is_default: boolean;
  source_type: string; // "git"
}

export interface PluginCatalogItem {
  name: string;
  description: string;
  category: string;
  author?: string;
  homepage?: string;
  version?: string;
  source_info?: Record<string, unknown>;
  sha?: string;
  installed: boolean;
}

export interface InstalledPlugin {
  name: string;
  version: string;
  description: string;
  source_id: string;
  source_info: Record<string, unknown>;
  components: { skills: string[]; commands: string[]; mcps: string[] };
  installed_at: string;
  sha: string;
  present: boolean;
}

export async function getPlugins(): Promise<InstalledPlugin[]> {
  const res = await fetch(`${httpBase()}/v1/plugins`);
  return (await res.json()).plugins ?? [];
}

export async function getPluginSources(): Promise<PluginSource[]> {
  const res = await fetch(`${httpBase()}/v1/plugins/sources`);
  return (await res.json()).sources ?? [];
}

export async function addPluginSource(
  name: string,
  url: string,
  sourceType = "git",
): Promise<{ ok: boolean; error?: string; source?: PluginSource }> {
  const res = await fetch(`${httpBase()}/v1/plugins/sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, url, source_type: sourceType }),
  });
  return res.json();
}

export async function updatePluginSource(
  sourceId: string,
  changes: Partial<PluginSource>,
): Promise<{ ok: boolean; error?: string; source?: PluginSource }> {
  const res = await fetch(`${httpBase()}/v1/plugins/sources/${sourceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function removePluginSource(
  sourceId: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/plugins/sources/${sourceId}`, {
    method: "DELETE",
  });
  return res.json();
}

export async function getPluginCatalog(
  sourceId: string,
): Promise<{
  ok: boolean;
  error?: string;
  source?: PluginSource;
  plugins?: PluginCatalogItem[];
  categories?: string[];
}> {
  const res = await fetch(`${httpBase()}/v1/plugins/sources/${sourceId}/catalog`);
  return res.json();
}

export async function installPlugin(
  sourceId: string,
  name: string,
): Promise<{ ok: boolean; error?: string; name?: string }> {
  const res = await fetch(`${httpBase()}/v1/plugins/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_id: sourceId, name }),
  });
  return res.json();
}

export async function uninstallPlugin(
  name: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/plugins/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  return res.json();
}

export async function checkPluginUpdates(): Promise<{
  ok: boolean;
  items: Array<{
    name: string;
    installed_sha: string;
    latest_sha: string;
    latest_version: string;
    up_to_date: boolean;
  }>;
}> {
  const res = await fetch(`${httpBase()}/v1/plugins/updates`);
  return res.json();
}

export async function updatePlugin(
  name: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/plugins/${encodeURIComponent(name)}/update`, {
    method: "POST",
  });
  return res.json();
}

// -- Digital-human plugin/command/subagent health (E4) ------------------------
export interface DepHealthItem {
  id: string;
  reason: string;
  installed: boolean;
}
export async function getDhpPluginsHealth(
  slug: string,
): Promise<{ ok: boolean; items?: DepHealthItem[] }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/${slug}/plugins-health`);
  return res.json();
}
export async function getDhpCommandsHealth(
  slug: string,
): Promise<{ ok: boolean; items?: DepHealthItem[] }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/${slug}/commands-health`);
  return res.json();
}
export async function getDhpSubagentsHealth(
  slug: string,
): Promise<{ ok: boolean; items?: DepHealthItem[] }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/${slug}/subagents-health`);
  return res.json();
}

// -- Browser login state (E5) -------------------------------------------------
// A persisted logged-in session for a site (Playwright storageState or pasted
// cookies). browser_open_url auto-loads the matching session when the agent
// visits a URL whose host matches a login entry.
export type LoginExpiryStatus = "no_state" | "session" | "valid" | "expiring" | "expired";

export interface LoginExpiry {
  status: LoginExpiryStatus;
  /** ISO UTC of the latest cookie expiry, when a real expiry exists. */
  expires_at?: string;
}

export interface BrowserLogin {
  id: string;
  url: string;
  label: string;
  storage_state_path: string;
  cookie_path: string;
  mode: "playwright" | "cookies";
  has_state: boolean;
  captured_at: string;
  notes: string;
  expiry?: LoginExpiry;
}

export async function getBrowserLogins(): Promise<BrowserLogin[]> {
  const res = await fetch(`${httpBase()}/v1/browser/logins`);
  return (await res.json()).logins ?? [];
}

export async function addBrowserLogin(
  url: string,
  label: string,
  mode = "playwright",
): Promise<{
  ok: boolean;
  error?: string;
  id?: string;
  entry?: BrowserLogin;
  playwright_available?: boolean;
}> {
  const res = await fetch(`${httpBase()}/v1/browser/logins`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, label, mode }),
  });
  return res.json();
}

export async function captureBrowserLogin(
  id: string,
): Promise<{
  ok: boolean;
  fallback?: string;
  mode?: string;
  id?: string;
  url?: string;
  error?: string;
}> {
  const res = await fetch(`${httpBase()}/v1/browser/logins/${encodeURIComponent(id)}/capture`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  return res.json();
}

export async function getBrowserLoginCaptureState(): Promise<{
  active: boolean;
  login_id?: string;
  url?: string;
  title?: string;
}> {
  const res = await fetch(`${httpBase()}/v1/browser/logins/capture-state`);
  return res.json();
}

export async function confirmBrowserLoginCapture(
  id: string,
): Promise<{ ok: boolean; error?: string; storage_state_path?: string; captured_at?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/browser/logins/${encodeURIComponent(id)}/capture/confirm`,
    { method: "POST" },
  );
  return res.json();
}

export async function cancelBrowserLoginCapture(): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/browser/logins/capture/cancel`, {
    method: "POST",
  });
  return res.json();
}

export async function saveBrowserLoginCookies(
  id: string,
  cookieJson: string,
): Promise<{ ok: boolean; error?: string; cookie_path?: string; captured_at?: string; count?: number }> {
  const res = await fetch(`${httpBase()}/v1/browser/logins/${encodeURIComponent(id)}/cookies`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookie_json: cookieJson }),
  });
  return res.json();
}

export async function removeBrowserLogin(id: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/browser/logins/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  return res.json();
}

export async function exportBrowserLogins(): Promise<{
  version: number;
  logins: Array<BrowserLogin & { _files?: Record<string, string> }>;
}> {
  const res = await fetch(`${httpBase()}/v1/browser/logins/export`);
  return res.json();
}

export async function importBrowserLogins(
  payload: string,
): Promise<{ ok: boolean; imported?: number; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/browser/logins/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payload }),
  });
  return res.json();
}

export interface LoginHealthItem {
  url: string;
  label: string;
  logged_in: boolean;
  expiry?: LoginExpiry;
}

export async function getDhpLoginsHealth(
  slug: string,
): Promise<{ ok: boolean; items?: LoginHealthItem[] }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/${slug}/logins-health`);
  return res.json();
}

// -- Rules (allow/deny/ask permission layer, E2) ------------------------------
// A rule = glob pattern (matched against the tool name) + action (allow/deny/ask).
export type RuleAction = "allow" | "deny" | "ask";

export interface Rule {
  id: string;
  pattern: string;
  action: RuleAction;
  reason: string;
  enabled: boolean;
}

export async function getRules(): Promise<Rule[]> {
  const res = await fetch(`${httpBase()}/v1/rules`);
  return (await res.json()).rules ?? [];
}

export async function addRule(
  pattern: string,
  action: RuleAction,
  reason = "",
): Promise<{ ok: boolean; error?: string; rule?: Rule }> {
  const res = await fetch(`${httpBase()}/v1/rules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pattern, action, reason }),
  });
  return res.json();
}

export async function updateRule(
  ruleId: string,
  changes: Partial<Rule>,
): Promise<{ ok: boolean; error?: string; rule?: Rule }> {
  const res = await fetch(`${httpBase()}/v1/rules/${ruleId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function removeRule(ruleId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/rules/${ruleId}`, {
    method: "DELETE",
  });
  return res.json();
}

// -- Hooks (pre_run/post_run, E2) ---------------------------------------------
// A hook fires on pre_run (before engine build, can skip) or post_run (after status
// determined). CRUD only — firing is internal to the scheduler.
export type HookEvent = "pre_run" | "post_run";

export interface Hook {
  id: string;
  name: string;
  event: HookEvent;
  match: string; // glob against task name; "*" = all
  command: string; // shell command or script path
  enabled: boolean;
}

export async function getHooks(): Promise<Hook[]> {
  const res = await fetch(`${httpBase()}/v1/hooks`);
  return (await res.json()).hooks ?? [];
}

export async function addHook(
  name: string,
  event: HookEvent,
  command: string,
  match = "*",
): Promise<{ ok: boolean; error?: string; hook?: Hook }> {
  const res = await fetch(`${httpBase()}/v1/hooks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, event, command, match }),
  });
  return res.json();
}

export async function updateHook(
  hookId: string,
  changes: Partial<Hook>,
): Promise<{ ok: boolean; error?: string; hook?: Hook }> {
  const res = await fetch(`${httpBase()}/v1/hooks/${hookId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function removeHook(hookId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/hooks/${hookId}`, {
    method: "DELETE",
  });
  return res.json();
}

// -- Commands (slash prompt templates, E3) ------------------------------------
// Read-only discovery of state_dir()/commands/<name>/COMMAND.md. The Composer "/" autocomplete
// lists these; selecting one fetches the full prompt_template and expands it into the input.
export interface Command {
  name: string;
  description: string;
  prompt_template?: string; // only present from getCommand(name)
  allowed_tools?: string[];
}

export async function getCommands(): Promise<Command[]> {
  const res = await fetch(`${httpBase()}/v1/commands`);
  return (await res.json()).commands ?? [];
}

export async function getCommand(name: string): Promise<Command | null> {
  const res = await fetch(`${httpBase()}/v1/commands/${encodeURIComponent(name)}`);
  if (!res.ok) return null;
  return (await res.json()).command ?? null;
}


// -- connectors ---------------------------------------------------------------
export interface ConnectorField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  help: string;
  placeholder: string;
}

// A message from a sender not (yet) on the allow-list — parked instead of dropped (§19).
export interface ParkedMessage {
  id: string;
  platform: string;
  chat_id: string;
  chat_name: string | null;
  user_id: string;
  user_name: string | null;
  chat_type: string;
  text: string;
  ts: number;
  team_id?: string | null; // workspace (managed Slack relay); null on manual Socket Mode
}

// One connected Slack workspace (managed relay is multi-workspace; ids are workspace-scoped,
// so each workspace carries its OWN allow-list).
export interface SlackWorkspace {
  team_id: string;
  account: string;
  domain?: string; // slack.com subdomain — unique even when display names collide
  allowed_users: string[];
  allow_all: boolean;
  allowed_user_names?: Record<string, string | null>;
  approval_owner_ids?: string[];
  approval_owner_names?: Record<string, string | null>;
  // Who installed this workspace (authed_user) — pre-added to the allow-list on
  // connect (UX-027); the GUI marks their chip "you" and keys the setup card copy.
  installer_user_id?: string;
  installer_name?: string;
}

// One connected GitHub App installation (managed relay is multi-installation;
// sender logins are global but each installation keeps its OWN allow-list).
export interface GithubInstallation {
  installation_id: string;
  account_login: string; // the org/user the App is installed on
  account_type: string; // "Organization" | "User"
  repo_selection: string; // "all" | "selected"
  github_login: string; // the connecting user's own login
  allowed_users: string[]; // sender logins allowed to trigger work
  allow_all: boolean;
}

export interface WeChatIlinkAccount {
  account_id: string;
  display_name: string;
  enabled: boolean;
  default: boolean;
  allowed_users: string[];
  allow_all: boolean;
  allowed_user_names?: Record<string, string | null>;
  needs_reauth: boolean;
  state: "connecting" | "live" | "reconnecting" | "auth_required" | "failed" | "offline" | string;
  retry_count: number;
  last_event_at: number | null;
  last_error: string;
}

export interface WeChatIlinkStatus {
  ok: boolean;
  state: "live" | "reconnecting" | "auth_required" | "offline" | string;
  accounts: WeChatIlinkAccount[];
}

export type WeChatIlinkQrState =
  | "waiting"
  | "scanned"
  | "confirmed"
  | "expired"
  | "failed"
  | "cancelled";

export interface WeChatIlinkQrAttempt {
  ok: boolean;
  error?: string;
  attempt_id?: string;
  status?: WeChatIlinkQrState;
  expires_at?: number;
  qr_content?: string;
  account_id?: string;
  display_name?: string;
}

// One connected HubSpot portal (multi-portal: `hubspot:portal:<hub_id>` profiles).
export interface HubSpotPortal {
  hub_id: string;
  name: string;
  sandbox: boolean;
  default: boolean;
  managed: boolean;
  access: "read" | "write" | ""; // consent tier granted ("" = manual token, unknown)
}

// One connected Google account (multi-account: `gmail:account:<email>` /
// `google_calendar:account:<email>` profiles — same shape for both).
export interface GmailAccount {
  email: string;
  default: boolean;
  managed: boolean;
  scopes: string;
  needs_reauth: boolean;
}

// "Never show agents" — enforced locally in the tool layer; agents see silent
// omissions, the user sees counts on tool cards + Activity rows.
export interface GmailFilters {
  senders: string[];
  labels: string[];
}

// One account of a generic multi-account connector (`<name>:account:<id>`
// profiles — Notion workspaces, PostHog projects, …). Gmail/Calendar predate
// the generic layer and keep their email-keyed shape above.
export interface AccountRow {
  account_id: string;
  name: string; // display identity captured at connect (workspace name, email, …)
  default: boolean;
  managed: boolean;
}

export interface Connector {
  name: string;
  title: string;
  icon: string;
  blurb: string;
  // Pre-connect detail page copy (UX-DECISIONS §38): optional About paragraph
  // (empty → group omitted) + honest Access bullets.
  about?: string;
  access?: string[];
  auth: string;
  two_way: boolean;
  // Chat-platform capability, narrower than two_way: sessions can subscribe to channels.
  channels: boolean;
  available: boolean;
  fields: ConnectorField[];
  instructions: string[];
  connected: boolean;
  account: string | null;
  enabled: boolean;
  brand_color: string; // hex brand color, e.g. "#611f69" (fallback gray "#6b7280")
  logo: string; // stable logo id keyed into the frontend registry (empty → fallback glyph)
  aliases?: string[]; // extra typeahead terms ("calendar" surfaces Outlook)
  mcp?: boolean; // MCP-backed one-click (vendor-hosted MCP + local OAuth — no cloud sign-in)
  allowed_users: string[]; // the allow-list (managed inline in the Connectors tab)
  allowed_user_names?: Record<string, string | null>; // id → display name (people directory)
  approval_owner_ids?: string[]; // Manual Slack: humans allowed to resolve approvals
  approval_owner_names?: Record<string, string | null>;
  recent?: RecentSender[]; // recently-seen senders on a connected two-way connector
  unauthorized?: ParkedMessage[]; // parked messages from unallowed senders (§19)
  tools: ConnectorTool[];
  managed: boolean; // one-click managed OAuth available (needs cloud sign-in)
  managed_paused?: boolean; // one-click temporarily off (e.g. Google CASA pending) — badge "Coming soon"
  managed_profile: boolean; // current profile came from managed OAuth (vs manual paste)
  mode?: string; // "relay" for the managed cloud path; "" for manual/token connect
  workspaces?: SlackWorkspace[]; // Slack only: connected workspaces (managed relay)
  // Gmail/Calendar: email-keyed rows; generic account connectors (notion,
  // attio, posthog, …): AccountRow; personal WeChat uses its live state row.
  accounts?: GmailAccount[] | AccountRow[] | WeChatIlinkAccount[];
  status?: WeChatIlinkStatus; // personal WeChat aggregate + per-account live state
  filters?: GmailFilters; // Gmail only: "Never show agents" senders/labels
  portals?: HubSpotPortal[]; // HubSpot only: connected portals (multi-portal)
  hidden_fields?: string[]; // HubSpot only: properties stripped from agent reads
  installations?: GithubInstallation[]; // GitHub only: App installations (managed relay)
}

// --- OpenWorker Cloud (optional sign-in; manual token paste always works) ---

export interface CloudStatus {
  signed_in: boolean;
  account: string;
  user_id: string;
  telemetry_enabled?: boolean; // Phase 5 opt-out; signed-out users send nothing regardless
}

/** Flip the product-telemetry preference (local; only meaningful when signed in). */
export async function setCloudTelemetry(
  enabled: boolean,
): Promise<{ ok: boolean; telemetry_enabled?: boolean }> {
  const res = await fetch(`${httpBase()}/v1/cloud/telemetry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return res.json();
}

export async function getCloudStatus(): Promise<CloudStatus> {
  const res = await fetch(`${httpBase()}/v1/cloud/status`);
  return res.json();
}

export async function cloudLogin(): Promise<{ ok: boolean }> {
  // The sidecar opens the system browser; the GUI just polls status after.
  const res = await fetch(`${httpBase()}/v1/cloud/login`, { method: "POST" });
  return res.json();
}

/** Poll cloud status until the browser sign-in lands (or the bound runs out).
 *
 * Fast 500ms polls for the first 20s — the moment the user finishes in the
 * browser they're staring at the app waiting for it to flip, and a 2s interval
 * reads as "sign-in is slow" (owner complaint, 2026-07-16) — then relaxes to 2s
 * for the long tail (~2min total). Calls `onDone` with the signed-in status, or
 * null when it timed out. Returns a cancel function (call on unmount). */
export function waitForCloudSignIn(
  onDone: (s: CloudStatus | null) => void,
): () => void {
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let polls = 0;
  const tick = async () => {
    polls += 1;
    const s = await getCloudStatus().catch(() => null);
    if (cancelled) return;
    if (s?.signed_in) return onDone(s);
    if (polls >= 90) return onDone(null); // 40×500ms + 50×2s ≈ 2min
    timer = setTimeout(tick, polls < 40 ? 500 : 2000);
  };
  timer = setTimeout(tick, 500);
  return () => {
    cancelled = true;
    if (timer) clearTimeout(timer);
  };
}

export async function cloudLogout(): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/cloud/logout`, { method: "POST" });
  return res.json();
}

export async function connectManaged(
  name: string,
  options?: { access?: "read" | "write" },
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(name)}/connect-managed`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // `access` names a broker-defined consent tier (hubspot read | write).
      // GitHub needs no flow choice: the broker is authorize-first — one connect
      // links an existing App installation or redirects on to the install page.
      body: JSON.stringify({
        ...(options?.access ? { access: options.access } : {}),
      }),
    },
  );
  return res.json();
}

/** One-click connect for an MCP-backed connector (monday, asana, jira): the sidecar
 * opens the vendor's sign-in in the browser (local OAuth, no cloud account needed);
 * poll getConnectors until the card flips to connected. */
export async function connectMcpBacked(name: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(name)}/mcp-connect`,
    { method: "POST" },
  );
  return res.json();
}

export interface ConnectorTool {
  name: string;
  label: string;
  kind: "read" | "write" | string;
  description: string;
  enabled: boolean;
  requires_approval: boolean;
}

export async function getConnectors(): Promise<Connector[]> {
  const res = await fetch(`${httpBase()}/v1/connectors`);
  return (await res.json()).connectors ?? [];
}

export async function connectConnector(
  name: string,
  fields: Record<string, string>,
): Promise<{ ok: boolean; account?: string; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fields }),
  });
  return res.json();
}

export async function disconnectConnector(name: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/disconnect`, {
    method: "POST",
  });
  return res.json();
}

export async function getWeChatIlinkAccounts(): Promise<WeChatIlinkAccount[]> {
  const res = await fetch(`${httpBase()}/v1/connectors/wechat_ilink/accounts`);
  return (await res.json()).accounts ?? [];
}

export async function getWeChatIlinkStatus(): Promise<WeChatIlinkStatus> {
  const res = await fetch(`${httpBase()}/v1/connectors/wechat_ilink/status`);
  return res.json();
}

export async function createWeChatIlinkQr(): Promise<WeChatIlinkQrAttempt> {
  const res = await fetch(`${httpBase()}/v1/connectors/wechat_ilink/qrcode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  return res.json();
}

export async function getWeChatIlinkQr(attemptId: string): Promise<WeChatIlinkQrAttempt> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/wechat_ilink/qrcode/${encodeURIComponent(attemptId)}`,
  );
  return res.json();
}

export async function cancelWeChatIlinkQr(attemptId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/wechat_ilink/qrcode/${encodeURIComponent(attemptId)}`,
    { method: "DELETE" },
  );
  return res.json();
}

export async function reauthWeChatIlinkAccount(accountId: string): Promise<WeChatIlinkQrAttempt> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/wechat_ilink/accounts/${encodeURIComponent(accountId)}/reauth`,
    { method: "POST" },
  );
  return res.json();
}

export async function disconnectWeChatIlinkAccount(
  accountId: string,
): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/wechat_ilink/accounts/${encodeURIComponent(accountId)}`,
    { method: "DELETE" },
  );
  return res.json();
}

export async function setDefaultWeChatIlinkAccount(
  accountId: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/wechat_ilink/accounts/${encodeURIComponent(accountId)}/default`,
    { method: "POST" },
  );
  return res.json();
}

export async function updateConnectorTools(
  name: string,
  enabled: Record<string, boolean>,
): Promise<{ ok: boolean; error?: string; tools?: Record<string, boolean> }> {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/tools`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return res.json();
}

export interface AuditEvent {
  id: number;
  timestamp: string;
  session_id: string;
  agent: string;
  workspace: string;
  connector: string;
  tool: string;
  stage: string;
  status: string;
  approval: string;
  args: Record<string, any>;
  result_preview: string;
  reason: string;
  resource: string;
}

export async function getAudit(params: {
  limit?: number;
  session_id?: string;
  connector?: string;
  tool?: string;
} = {}): Promise<AuditEvent[]> {
  const q = new URLSearchParams();
  if (params.limit) q.set("limit", String(params.limit));
  if (params.session_id) q.set("session_id", params.session_id);
  if (params.connector) q.set("connector", params.connector);
  if (params.tool) q.set("tool", params.tool);
  const res = await fetch(`${httpBase()}/v1/audit${q.toString() ? "?" + q.toString() : ""}`);
  return (await res.json()).events ?? [];
}

export interface BrowserState {
  open: boolean;
  url: string;
  title: string;
  status: string;
  last_action: string;
  last_result: string;
  last_error: string;
  screenshot_data_url: string;
  updated_at: string | null;
  controls: any[];
}

export async function getBrowserState(): Promise<BrowserState> {
  const res = await fetch(`${httpBase()}/v1/browser/state`);
  return res.json();
}

export async function takeBrowserScreenshot(): Promise<BrowserState & { ok?: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/browser/screenshot`, { method: "POST" });
  return res.json();
}

export async function closeBrowser(): Promise<{ ok?: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/browser/close`, { method: "POST" });
  return res.json();
}

// -- settings (model API key, default model, onboarding) ----------------------
export interface SurfaceVisibility {
  cowork: boolean; // always true
  chat: boolean;
  code: boolean;
}

export interface ModelSettings {
  provider: string;
  model: string;
  models: string[];
  has_key: boolean;
  model_ready: boolean; // can the default model's provider actually run (any provider)?
  source: "env" | "store" | null;
  onboarded: boolean;
  surfaces: SurfaceVisibility;
  scratch_base: string;
  secrets_path: string;  // OS-native on-disk location the server reports (not hardcoded)
  // Sidebar layout preference (§7): "flat" = the persona accordions / today's list; "grouped" =
  // bounded per-persona cards. Defaults to "flat" (absent → flat) so the GUI is robust to an older
  // backend that hasn't shipped the field yet.
  nav_layout?: "flat" | "grouped";
  // Sidebar: sessions shown per group before "Show more" (default 5, 1–50).
  sessions_peek?: number;
  // Composer: show the context-window fill bar (default FALSE; absent → the chip shows
  // the session total). The usage popover keeps both numbers regardless.
  context_bar?: boolean;
  // Curated-matrix display names ({full id → "GLM-5.2 · via Together"}); custom models absent.
  model_labels?: Record<string, string>;
  // {full id → context window in tokens}, verified matrix entries only — drives the
  // composer's context-fill meter (absent id → the meter hides). Optional for older backends.
  model_context_windows?: Record<string, number>;
  // Token savings (PDF attachments): fallback for models without native PDF support,
  // and attach-time thresholds. Optional so the GUI is robust to an older backend.
  pdf_fallback?: "text" | "images";
  pdf_max_pages?: number; // default 20, 1–100
  pdf_max_mb?: number; // default 10, 1–10
  // Auto-compaction of long histories (OPE-27): trigger = min(threshold% × context
  // window, cap tokens); model pins the summarizer ("" → the session's own model).
  // Optional so the GUI is robust to an older backend.
  compaction_threshold_pct?: number; // default 0.8, 0.10–0.95
  compaction_cap_tokens?: number; // default 250000
  compaction_model?: string;
}

export interface PdfSettings {
  pdf_fallback: "text" | "images";
  pdf_max_pages: number;
  pdf_max_mb: number;
}

/** Persist the Token-savings PDF settings (fallback mode + attach thresholds). */
export async function setPdfSettings(
  patch: Partial<PdfSettings>,
): Promise<{ ok: boolean; error?: string } & Partial<PdfSettings>> {
  const res = await fetch(`${httpBase()}/v1/settings/pdf`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return res.json();
}

export interface CompactionSettings {
  compaction_threshold_pct: number;
  compaction_cap_tokens: number;
  compaction_model: string;
}

/** Persist the auto-compaction overrides (threshold %, token cap, summarizer model). */
export async function setCompactionSettings(
  patch: Partial<CompactionSettings>,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/compaction`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return res.json();
}

/** Local page/size probe for a PDF data URL — the composer's attach-time threshold check. */
export async function inspectPdf(
  dataUrl: string,
): Promise<{ ok: boolean; pages?: number; bytes?: number; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/attachments/inspect-pdf`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl }),
  });
  return res.json();
}

/** Persist whether the composer shows the context-window fill bar. */
export async function setContextBar(
  shown: boolean,
): Promise<{ ok: boolean; context_bar?: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/context-bar`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ context_bar: shown }),
  });
  return res.json();
}

/** Persist how many sessions a sidebar group shows before "Show more". */
export async function setSessionsPeek(
  n: number,
): Promise<{ ok: boolean; sessions_peek?: number; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/sessions-peek`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessions_peek: n }),
  });
  return res.json();
}

export async function setScratchBase(
  path: string,
): Promise<{ ok: boolean; error?: string; scratch_base?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/scratch-base`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return res.json();
}

export async function setSurfaces(
  flags: { chat?: boolean; code?: boolean },
): Promise<{ ok: boolean; surfaces: SurfaceVisibility }> {
  const res = await fetch(`${httpBase()}/v1/settings/surfaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(flags),
  });
  return res.json();
}

/** Persist the sidebar layout preference (flat ↔ grouped-by-persona); read back from getSettings. */
export async function setNavLayout(
  layout: "flat" | "grouped",
): Promise<{ ok: boolean; nav_layout?: "flat" | "grouped"; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/nav-layout`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nav_layout: layout }),
  });
  return res.json();
}

// Fired after a cloud sign-in/out completes so the account row (§26) refreshes without
// waiting for the next window focus.
export const CLOUD_CHANGED = "coworker:cloud-changed";
export function announceCloudChanged() {
  window.dispatchEvent(new CustomEvent(CLOUD_CHANGED));
}

// Fired the first time Inbox machinery is engaged (an item parks, or a session goes
// Unattended) — the account row's inbox chip unlocks stickily on it (§26).
export const INBOX_UNLOCK = "coworker:inbox-unlock";
export function announceInboxUnlock() {
  window.dispatchEvent(new CustomEvent(INBOX_UNLOCK));
}

// -- Personas -----------------------------------------------------------------

// Fired after any persona mutation (enable/disable/install/delete) so always-mounted
// consumers (the sidebar's new-session picker) refetch instead of going stale.
export const PERSONAS_CHANGED = "coworker:personas-changed";
function announcePersonasChanged() {
  window.dispatchEvent(new CustomEvent(PERSONAS_CHANGED));
}

export interface Persona {
  id: string;
  name: string;
  icon: string;
  tagline: string;
  needs_workspace: boolean;
  builtin: boolean;
  family: string;
  workspace: string; // "git" | "project" | "deliverable" | "none" — drives project-scoping
  tools: string[];
  enabled: boolean;
  surfaced: boolean;
  default: boolean;
}

export interface PersonaConsent {
  id: string;
  name: string;
  description: string;
  tools: string[];
  risk: string[];
  connectors: boolean;
  mcp: string[];
  messaging: boolean;
  recommended_mode: string;
  recommended_models: string[];
  source: string | null;
  builtin: boolean;
}

export async function getPersonas(): Promise<Persona[]> {
  const res = await fetch(`${httpBase()}/v1/personas`);
  return (await res.json()).personas;
}

export async function updatePersona(
  id: string,
  body: { enabled?: boolean; surfaced?: boolean; default?: boolean },
): Promise<{ ok: boolean; personas?: Persona[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  if (out.ok !== false) announcePersonasChanged();
  return out;
}

/** Uninstall a non-builtin persona (its snapshot + state). Local; works signed out. */
export async function deletePersona(
  id: string,
): Promise<{ ok: boolean; personas?: Persona[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// A curated persona card from the cloud gallery (metadata only — the manifest
// is fetched server-side at install and runs through the normal consent flow).
export interface GalleryPersona {
  slug: string;
  version: number;
  name: string;
  icon: string;
  tagline: string;
  description: string;
  family: string;
  workspace: string;
  publisher: string;
  recommended_connectors: string[];
  risk_summary: string;
  featured?: boolean; // publisher-flagged for the gallery's featured carousel
}

export async function getCloudGallery(): Promise<{
  ok: boolean;
  personas: GalleryPersona[];
  error?: string;
}> {
  const res = await fetch(`${httpBase()}/v1/cloud/gallery`);
  return res.json();
}

// Solo page for one gallery coworker. `capabilities` is the desktop's own
// consent summary derived from the manifest (same parser as install), so the
// page shows exactly what installing would ask the user to approve.
export interface GalleryDetail {
  ok: boolean;
  error?: string;
  card?: GalleryPersona & { pitch_markdown: string };
  capabilities?: {
    tools: string[];
    risk: string[];
    connectors: boolean;
    mcp: string[];
    messaging: boolean;
    recommended_mode: string;
    recommended_models: string[];
  };
  recommends?: { kind: string; ref: string; reason: string; tier: string }[];
}

export async function getCloudGalleryDetail(slug: string): Promise<GalleryDetail> {
  const res = await fetch(`${httpBase()}/v1/cloud/gallery/${encodeURIComponent(slug)}`);
  return res.json();
}

export async function installPersona(
  body: { dir?: string; git_url?: string; gallery_slug?: string },
): Promise<{ ok: boolean; consent?: PersonaConsent[]; personas?: Persona[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// -- Persona marketplace sources (批次 E4 后续) -------------------------------
// Git repos of *.md persona manifests the user can browse + install from. Mirrors
// plugin sources. No builtin source today (no Anthropic-official persona marketplace);
// the user adds their own. Install reuses the consent path — lands disabled.
export interface PersonaSource {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  is_default: boolean;
  source_type: string; // "git"
}

export interface PersonaCatalogItem {
  id: string;
  name: string;
  tagline: string;
  description: string;
  icon: string;
  family: string; // "code" | "knowledge"
  file: string; // repo-relative manifest path
  installed: boolean;
}

export async function getPersonaSources(): Promise<PersonaSource[]> {
  const res = await fetch(`${httpBase()}/v1/personas/sources`);
  return (await res.json()).sources ?? [];
}

export async function addPersonaSource(
  name: string,
  url: string,
  sourceType = "git",
): Promise<{ ok: boolean; error?: string; source?: PersonaSource }> {
  const res = await fetch(`${httpBase()}/v1/personas/sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, url, source_type: sourceType }),
  });
  return res.json();
}

export async function updatePersonaSource(
  sourceId: string,
  changes: Partial<PersonaSource>,
): Promise<{ ok: boolean; error?: string; source?: PersonaSource }> {
  const res = await fetch(`${httpBase()}/v1/personas/sources/${sourceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function removePersonaSource(
  sourceId: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/sources/${sourceId}`, {
    method: "DELETE",
  });
  return res.json();
}

export async function getPersonaCatalog(
  sourceId: string,
): Promise<{
  ok: boolean;
  error?: string;
  source?: PersonaSource;
  personas?: PersonaCatalogItem[];
}> {
  const res = await fetch(`${httpBase()}/v1/personas/sources/${sourceId}/catalog`);
  return res.json();
}

export async function installPersonaFromSource(
  sourceId: string,
  personaId: string,
): Promise<{ ok: boolean; consent?: PersonaConsent; personas?: Persona[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/sources/${sourceId}/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ persona_id: personaId }),
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// -- Persona detail + connection defaults (§5) --------------------------------
// A persona's declared recommendation (manifest `recommends`): a connector or MCP server it works
// best with, with a reason + tier (core/optional). `connected` is annotated server-side from the
// connector list so the detail page can show connect state without a second round-trip.
export interface PersonaRecommendation {
  kind: string; // "connector" | "mcp" | …
  ref: string; // connector id (e.g. "github") or mcp/server name
  reason: string;
  tier: string; // "core" | "optional"
  connected: boolean;
}

// A persona-default connection (the middle of the §4 hierarchy): for a connected connector, whether
// new sessions of this persona get it enabled by default.
export interface PersonaDefaultConnection {
  connector: string; // connector id
  enabled: boolean; // persona-default on/off
  connected: boolean; // is the account actually connected (else the toggle is disabled)
}

export interface PersonaDetail {
  id: string;
  name: string;
  icon: string;
  tagline: string;
  description: string;
  enabled: boolean; // persona on/off (shown in the picker)
  tools: string[];
  recommended_models: string[];
  default_permission_mode: string;
  workspace: string;
  recommends: PersonaRecommendation[];
  default_connections: PersonaDefaultConnection[];
}

export async function getPersonaDetail(id: string): Promise<PersonaDetail> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}`);
  return res.json();
}

/** Set a persona-default connection (new sessions of this persona get it on/off by default). */
export async function setPersonaConnection(
  id: string,
  connector: string,
  enabled: boolean,
): Promise<{ ok: boolean; default_connections?: PersonaDefaultConnection[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}/connections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connector, enabled }),
  });
  return res.json();
}

/** Enable/disable the persona (whether it surfaces in the new-session picker). */
export async function setPersonaEnabled(
  id: string,
  enabled: boolean,
): Promise<{ ok: boolean; personas?: Persona[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}/enable`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// -- Per-session connections (Sources bar + drawer, §6) -----------------------
// An effective-enabled connector for a session, with a short human detail (e.g. "#ocw-test · DMs").
// `enabled` reflects the session override/persona default so the drawer toggle shows correct state.
export interface SessionConnectedConnector {
  connector: string;
  enabled: boolean;
  detail: string;
}

// A persona-recommended connector not yet connected (drives the `⚠ N` attention count).
export interface SessionRecommendedConnector {
  connector: string;
  reason: string;
  tier: string;
  connected: boolean;
}

export interface SessionConnections {
  connected: SessionConnectedConnector[];
  recommended: SessionRecommendedConnector[];
  attention: number; // ⚠ count = recommended connectors not yet connected
}

/** `persona` = the active persona hint — required for brand-new sessions (no server-side
 * record yet), otherwise the view resolves to the default persona's defaults/recommends. */
export async function getSessionConnections(
  sessionId: string,
  persona?: string,
): Promise<SessionConnections> {
  const q = persona ? `?persona=${encodeURIComponent(persona)}` : "";
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/connections${q}`,
  );
  return res.json();
}

/**
 * Set a per-session connection override (mute/unmute a connector for THIS session). Pass
 * `clear: true` to drop the override and inherit the persona default again.
 */
export async function setSessionConnection(
  sessionId: string,
  connector: string,
  enabled: boolean,
  clear = false,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/connections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connector, enabled, ...(clear ? { clear: true } : {}) }),
  });
  return res.json();
}

// -- Inbox + Unattended -------------------------------------------------------
export interface InboxItem {
  id: string;
  session_id: string;
  kind: "approval" | "question" | "notification" | "directory" | "plan";
  title: string;
  body: string;
  state: "pending" | "resolved";
  resolution: string | null;
  inbox: string;
  created_at: string;
  resolved_at: string | null;
  visibility?: "inline" | "inbox";
  // Question metadata (ask_user): quick-reply choices + a free-text escape.
  options?: string[];
  allow_text?: boolean;
  multi?: boolean;
  // Kind-specific payload (directory: {path, writable}; …).
  data?: Record<string, any>;
  // Originating-session context (server-joined) so the Inbox is self-contained.
  session_title?: string;
  session_agent?: string | null;
  session_workspace?: string | null;
  session_exists?: boolean;
}

export async function getInbox(sessionId?: string, state?: string): Promise<InboxItem[]> {
  const q = new URLSearchParams();
  if (sessionId) q.set("session_id", sessionId);
  if (state) q.set("state", state);
  const res = await fetch(`${httpBase()}/v1/inbox?${q.toString()}`);
  return (await res.json()).items;
}

export async function resolveInboxItem(
  id: string,
  resolution: string,
): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/inbox/${encodeURIComponent(id)}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resolution }),
  });
  return res.json();
}

// -- channel subscriptions (view-only) ----------------------------------------
export interface Subscription {
  session_id: string;
  session_title: string;
  agent: string;
  channel: string;
  channel_name?: string | null; // resolved display name ("ocw-test"); address stays the id
  routing_target: string | null;
  collision: boolean; // inbound subscription == outbound Inbox routing on the same channel
}

export interface RecentChannel {
  channel: string;
  name?: string | null; // resolved display name, e.g. "ocw-test" (falls back to the address)
  last_from: string | null;
  last_text: string | null;
}

export async function getSubscriptions(): Promise<Subscription[]> {
  const res = await fetch(`${httpBase()}/v1/subscriptions`);
  return (await res.json()).subscriptions ?? [];
}

// -- inbox routing (where Unattended approvals/questions get mirrored) ---------
export interface InboxBinding {
  name: string;
  channel: string | null; // platform, e.g. "slack" (null = in-app Inbox only)
  target: string; // chat_id, e.g. "C0BEJNCQQ8Y"
}

export async function getInboxRouting(): Promise<InboxBinding[]> {
  const res = await fetch(`${httpBase()}/v1/inbox/routing`);
  return (await res.json()).bindings ?? [];
}

export async function setInboxBinding(
  name: string,
  channel: string | null,
  target: string,
): Promise<{ ok: boolean; bindings?: InboxBinding[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/inbox/routing/binding`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, channel, target }),
  });
  return res.json();
}

export interface UnroutedItem {
  source: string;
  sender: string;
  text: string;
  reason: string;
  ts: number;
}

export async function getUnrouted(): Promise<UnroutedItem[]> {
  const res = await fetch(`${httpBase()}/v1/unrouted`);
  return (await res.json()).items ?? [];
}

export async function getRecentChannels(): Promise<RecentChannel[]> {
  const res = await fetch(`${httpBase()}/v1/channels/recent`);
  return (await res.json()).channels ?? [];
}

export async function subscribeChannel(
  sessionId: string,
  channel: string,
): Promise<{ ok: boolean; channel?: string; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/subscriptions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, channel }),
  });
  return res.json();
}

export async function unsubscribeChannel(
  sessionId: string,
  channel: string,
): Promise<{ ok: boolean; removed?: boolean }> {
  const res = await fetch(`${httpBase()}/v1/subscriptions/remove`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, channel }),
  });
  return res.json();
}

export async function getUnattended(sessionId: string): Promise<boolean> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/unattended`,
  );
  return (await res.json()).unattended;
}

export async function setUnattended(
  sessionId: string,
  unattended: boolean,
): Promise<{ ok: boolean; unattended: boolean }> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/unattended`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ unattended }),
    },
  );
  return res.json();
}

export async function getSettings(): Promise<ModelSettings> {
  const res = await fetch(`${httpBase()}/v1/settings`);
  return res.json();
}

export async function setModelKey(
  apiKey: string,
): Promise<{ ok: boolean; error?: string; has_key?: boolean; source?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/model-key`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey }),
  });
  return res.json();
}

export async function setDefaultModel(
  model: string,
): Promise<{ ok: boolean; error?: string; model?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/default-model`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return res.json();
}

export async function addModel(model: string): Promise<ModelSettings & { ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/models/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return res.json();
}

export async function removeModel(model: string): Promise<ModelSettings & { ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/settings/models/remove`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return res.json();
}

export async function setOnboarded(value: boolean): Promise<{ ok: boolean; onboarded: boolean }> {
  const res = await fetch(`${httpBase()}/v1/settings/onboarded`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  return res.json();
}

// -- model providers (OpenAI, Ollama, …) --------------------------------------
export interface ProviderField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  help: string;
  placeholder: string;
  default?: string; // pre-filled editable value (e.g. an OpenAI-compatible vendor's endpoint)
  // Non-empty → segmented choice, not a text input. tag = tiny badge ("Easiest");
  // desc = one-liner atop the method panel; command = copyable terminal command.
  choices?: { value: string; label: string; tag?: string; desc?: string; command?: string }[];
  show_when?: Record<string, string> | null; // render only while these fields hold these values
}

export interface ProviderInfo {
  name: string;
  title: string;
  needs_key: boolean;
  fields: ProviderField[];
  configured: boolean;
  values: Record<string, string>; // non-secret stored values (e.g. base_url), for prefilling
  suggested_models: string[]; // bare model-name suggestions for the "add model" datalist
  recommended_model: string | null; // pre-filled default for this provider (e.g. qwen3-coder:30b)
  blurb?: string; // one-line note under the title ("Uses X's OpenAI-compatible API…")
  key_set_at?: string | null; // ISO date the key was last (re)saved — absent for env-only config
  last_used_at?: number | null; // epoch secs the provider last served a completion
}

export async function getProviders(): Promise<ProviderInfo[]> {
  const res = await fetch(`${httpBase()}/v1/providers`);
  return res.json();
}

export async function setProvider(
  name: string,
  fields: Record<string, string>,
): Promise<{ ok: boolean; error?: string; provider?: string; recommended_model?: string | null }> {
  const res = await fetch(`${httpBase()}/v1/providers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, fields }),
  });
  return res.json();
}

/** Forget a provider's stored config (Settings ▸ Models "Remove key…"). */
export async function removeProvider(name: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/providers/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  return res.json();
}

/** Live read-only credential check (does NOT save the key). Triggered by the user's "Test" click. */
export async function verifyProvider(
  name: string,
  fields: Record<string, string>,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/providers/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, fields }),
  });
  return res.json();
}

/** Client-side provider guess from an API key's shape (mirrors the server's detect_provider). */
export function detectProvider(apiKey: string): string | null {
  const key = (apiKey || "").trim();
  if (!key) return null;
  if (key.startsWith("sk-ant-")) return "anthropic";
  if (key.startsWith("sk-or-")) return "openrouter";
  if (key.startsWith("AIza")) return "gemini";
  if (key.startsWith("sk-") || key.startsWith("sk_")) return "openai";
  return null;
}

// -- super-agent --------------------------------------------------------------
export interface RecentSender {
  user_id: string;
  user_name: string | null;
  chat_id: string;
  chat_type: string;
  target: string;
  authorized: boolean;
  team_id?: string | null; // workspace (managed relay); null on manual Socket Mode
}

// -- direct-message routing ---------------------------------------------------
export async function getDmRoute(): Promise<string | null> {
  const res = await fetch(`${httpBase()}/v1/messaging/dm-route`);
  return (await res.json()).dm_session ?? null;
}

export async function setDmRoute(sessionId: string): Promise<{ ok: boolean; dm_session: string | null }> {
  const res = await fetch(`${httpBase()}/v1/messaging/dm-route`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  return res.json();
}

// -- automations (scheduled tasks) --------------------------------------------
export interface Automation {
  id: string;
  title: string;
  instructions: string;
  schedule: string;
  schedule_raw?: { kind: string; cron?: string | null; fire_at?: string | null; timezone?: string };
  workspace: string;
  agent: string;
  enabled: boolean;
  next_run: number | null;
  last_run: number | null;
  last_status: string | null;
  run_count: number;
  notify_on_completion: boolean;
  // UX-023 sidebar badges: runs started since the user last opened this automation's
  // detail; `unseen_failed` = the newest unseen run errored (danger tint).
  unseen_runs?: number;
  unseen_failed?: boolean;
  seen_runs_at?: number;
  // Standing scoped approvals (§25): target-bound rules this automation may exercise
  // without asking. `entry` is the raw record entry — the revoke handle; `target` is
  // null for legacy name-only entries.
  always_allowed: { entry: string; tool: string; target: string | null }[];
}

export interface AutomationRun {
  run_id: string;
  task_id: string;
  session_id: string;
  started_at: number;
  finished_at: number | null;
  status: string;
  result_text: string | null;
  artifacts: string[];
  error: string | null;
  trigger: string;
}

export async function getAutomations(): Promise<Automation[]> {
  const res = await fetch(`${httpBase()}/v1/automations`);
  return (await res.json()).tasks ?? [];
}

// Fired after any automation mutation the sidebar should reflect immediately
// (mark-seen, create, delete) — its poll covers the rest.
export const AUTOMATIONS_CHANGED = "coworker:automations-changed";
export function announceAutomationsChanged() {
  window.dispatchEvent(new CustomEvent(AUTOMATIONS_CHANGED));
}

/** App-wide event stream (/ws/events): session-independent server pushes — today
 * automation_run_started (the UX-026 toast). Quietly reconnects while the app is
 * open; the returned cleanup stops it for good. */
export function connectEvents(
  onEvent: (msg: { type: string; data?: Record<string, unknown> }) => void
): () => void {
  let ws: WebSocket | null = null;
  let timer: number | null = null;
  let closed = false;
  const open = () => {
    if (closed) return;
    ws = openWebSocket(`${wsBase()}/ws/events`);
    ws.onmessage = (e) => {
      try {
        onEvent(JSON.parse(e.data));
      } catch {
        /* malformed frame — ignore */
      }
    };
    ws.onclose = () => {
      if (closed) return;
      // A 403 (sidecar restarted, token rotated) closes the socket before any REST 401 can
      // refresh the override. Proactively refresh the dev token, then reconnect — without this
      // the loop would keep using the stale token and 403-loop forever.
      const reconnect = () => {
        if (!closed) timer = window.setTimeout(open, 5000);
      };
      if (!devTokenOverride) {
        refreshDevToken().finally(reconnect);
      } else {
        reconnect();
      }
    };
  };
  open();
  return () => {
    closed = true;
    if (timer !== null) window.clearTimeout(timer);
    ws?.close();
  };
}

/** Advance the automation's seen mark — clears its unseen-runs badge (UX-023). */
export async function markAutomationSeen(id: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/automations/${id}/seen`, { method: "POST" });
  return res.json();
}

export async function createAutomation(payload: {
  title: string;
  instructions: string;
  cron?: string;
  fire_at?: string;
  timezone?: string;
  // §25 standing grants (the creating surface rendered them; submit IS the consent).
  // Only target-bound write entries survive server-side validation.
  permissions?: { tool: string; target: string; access: "read" | "write" }[];
}): Promise<{ ok: boolean; error?: string; task?: Automation }> {
  const res = await fetch(`${httpBase()}/v1/automations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res.json();
}

export async function getAutomation(id: string): Promise<{ task: Automation; runs: AutomationRun[] }> {
  const res = await fetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}`);
  return res.json();
}

export async function updateAutomation(id: string, changes: Record<string, any>) {
  const res = await fetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function deleteAutomation(id: string) {
  const res = await fetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}`, { method: "DELETE" });
  return res.json();
}

export interface PreparedRun {
  ok: boolean;
  error?: string;
  run_id: string;
  session_id: string;
  workspace: string;
  agent: string;
  prompt: string;
}

/** Prepare a live manual run: returns the session to open + the opening prompt to send. */
export async function runAutomation(id: string): Promise<PreparedRun> {
  const res = await fetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}/run`, { method: "POST" });
  return res.json();
}

/** Mark a manual run complete after its first turn finished. */
export async function finalizeAutomationRun(id: string, runId: string) {
  const res = await fetch(
    `${httpBase()}/v1/automations/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/finalize`,
    { method: "POST" },
  );
  return res.json();
}

export async function allowUser(
  name: string,
  userId: string,
  teamId?: string | null,
  displayName?: string,
) {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/allow`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      ...(teamId ? { team_id: teamId } : {}),
      // Directory picks carry the display name so the chip is readable at once.
      ...(displayName ? { name: displayName } : {}),
    }),
  });
  return res.json();
}

// One workspace member from the roster (people picker; users:read, cached locally).
export interface SlackMember {
  id: string;
  name: string;
  handle: string;
  guest: boolean;
}

// One channel from the workspace roster. Private channels appear only where the
// bot is a member (Slack API constraint); is_member=false → "invite @OpenWorker" hint.
export interface SlackChannelEntry {
  id: string;
  name: string;
  is_private: boolean;
  is_member: boolean;
}

/** Workspace member roster for the people picker (teamId "default" = manual Socket Mode). */
export async function getSlackDirectory(
  teamId: string,
  q = "",
): Promise<{ ok: boolean; error?: string; members?: SlackMember[] }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/slack/workspaces/${encodeURIComponent(teamId)}/directory?q=${encodeURIComponent(q)}`,
  );
  return res.json();
}

/** Channel roster for the channel typeahead (name → id resolution). */
export async function getSlackChannels(
  teamId: string,
  q = "",
): Promise<{ ok: boolean; error?: string; channels?: SlackChannelEntry[] }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/slack/workspaces/${encodeURIComponent(teamId)}/channels?q=${encodeURIComponent(q)}`,
  );
  return res.json();
}

/** Resolve a parked unauthorized message (§19): dismiss / allow / allow_deliver. */
export async function resolveUnauthorized(
  name: string,
  itemId: string,
  action: "dismiss" | "allow" | "allow_deliver",
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(name)}/unauthorized/${encodeURIComponent(itemId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    },
  );
  return res.json();
}

export async function disallowUser(name: string, userId: string, teamId?: string | null) {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/disallow`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(teamId ? { user_id: userId, team_id: teamId } : { user_id: userId }),
  });
  return res.json();
}

export async function addSlackApprovalOwner(
  userId: string,
  displayName?: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/slack/approval-owners/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      ...(displayName ? { name: displayName } : {}),
    }),
  });
  return res.json();
}

export async function removeSlackApprovalOwner(
  userId: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/slack/approval-owners/remove`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });
  return res.json();
}

/** Stop relaying one managed Slack workspace (the app stays installed in Slack). */
export async function disconnectSlackWorkspace(teamId: string): Promise<{ ok: boolean; error?: string; remaining_workspaces?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/slack/workspaces/${encodeURIComponent(teamId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE Gmail mailbox; the default pointer moves to the next account. */
export async function disconnectGmailAccount(email: string): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/gmail/accounts/${encodeURIComponent(email)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setGmailDefaultAccount(email: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/gmail/accounts/${encodeURIComponent(email)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE Google Calendar account; the default pointer moves to the next one. */
export async function disconnectGcalAccount(email: string): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/google_calendar/accounts/${encodeURIComponent(email)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setGcalDefaultAccount(email: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/google_calendar/accounts/${encodeURIComponent(email)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE account of a generic multi-account connector (notion, attio,
 * posthog, …); the default pointer moves to the next account. */
export async function disconnectAccount(connector: string, accountId: string): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(connector)}/accounts/${encodeURIComponent(accountId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setDefaultAccount(connector: string, accountId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(connector)}/accounts/${encodeURIComponent(accountId)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Replace the "Never show agents" lists (senders and/or labels; omit to keep). */
export async function setGmailFilters(filters: { senders?: string[]; labels?: string[] }): Promise<{ ok: boolean; filters?: GmailFilters; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/gmail/filters`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
  });
  return res.json();
}

// GitHub relay health, the Slack three-layer shape: shared relay socket /
// cloud sign-in / per-installation token health (+ missed-event counts).
export interface GithubStatus {
  ok: boolean;
  mode: string;
  relay: { state: string; reconnects: number; last_event_at: number | null; last_error: string };
  signed_in: boolean;
  installs: Record<string, { token_ok: boolean }>;
  missed: Record<string, number>;
}

export async function getGithubStatus(): Promise<GithubStatus> {
  const res = await fetch(`${httpBase()}/v1/connectors/github/status`);
  return res.json();
}

/** Stop relaying ONE GitHub App installation to this computer. */
export async function disconnectGithubInstallation(installationId: string): Promise<{ ok: boolean; error?: string; remaining_installs?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/github/installations/${encodeURIComponent(installationId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE HubSpot portal; the default pointer moves to the next portal. */
export async function disconnectHubSpotPortal(hubId: string): Promise<{ ok: boolean; error?: string; remaining_portals?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/hubspot/portals/${encodeURIComponent(hubId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setHubSpotDefaultPortal(hubId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/hubspot/portals/${encodeURIComponent(hubId)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Replace the hidden-fields denylist (properties stripped from agent reads). */
export async function setHubSpotHiddenFields(fields: string[]): Promise<{ ok: boolean; hidden_fields?: string[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/hubspot/hidden-fields`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hidden_fields: fields }),
  });
  return res.json();
}

/** Slack health, three honest layers: relay socket / cloud sign-in / per-team tokens. */
export interface SlackStatus {
  mode: string; // "relay" | "" (manual/off)
  relay: {
    state: "live" | "reconnecting" | "offline";
    reconnects: number;
    last_event_at: number | null;
    last_error: string;
  };
  signed_in: boolean;
  teams: Record<string, { token_ok: boolean }>;
}

export async function getSlackStatus(): Promise<SlackStatus> {
  const res = await fetch(`${httpBase()}/v1/connectors/slack/status`);
  return res.json();
}

// 企微自建应用健康：连接状态 + corpid/agent_id + 加密模式（来自 WeComAppAdapter.status()）。
export interface WecomStatus {
  connected: boolean;
  configured?: boolean;
  corpid?: string;
  agent_id?: string;
  has_token?: boolean;
  has_aes_key?: boolean;
  encrypted?: boolean;
  inbound_state?: "encrypted" | "outbound_only" | string;
  needs_migration?: boolean;
}

export async function getWecomStatus(): Promise<WecomStatus> {
  try {
    const res = await fetch(`${httpBase()}/v1/connectors/wecom/status`);
    return res.json();
  } catch {
    return { connected: false };
  }
}

export type Handlers = {
  onEvent: (event: WsEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

export class Session {
  private ws: WebSocket;
  // Payloads sent before the socket finished opening, replayed on `onopen`. Belt-and-suspenders
  // against the first message being dropped if the user sends in the connect window.
  private outbox: object[] = [];
  // P1-06: auto-reconnect on unexpected close. `closed` marks an intentional teardown (close()),
  // which suppresses reconnect. Backoff is exponential with jitter, capped at 30s.
  private closed = false;
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;
  private readonly maxReconnectDelayMs = 30_000;
  private readonly baseReconnectDelayMs = 1_000;

  constructor(
    private sessionId: string,
    private workspace: string,
    private agent: string,
    private handlers: Handlers,
  ) {
    this.ws = this.open();
  }

  private url(): string {
    const q = `?workspace=${encodeURIComponent(this.workspace)}&agent=${encodeURIComponent(this.agent)}`;
    return `${wsBase()}/ws/session/${this.sessionId}${q}`;
  }

  private open(): WebSocket {
    const ws = openWebSocket(this.url());
    ws.onmessage = (e) => this.handlers.onEvent(JSON.parse(e.data));
    ws.onopen = () => {
      this.reconnectAttempt = 0;
      this.flush();
      this.handlers.onOpen?.();
    };
    ws.onclose = () => {
      if (this.closed) return;
      this.handlers.onClose?.();
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      // The browser will fire onclose after onerror; reconnect is handled there.
    };
    return ws;
  }

  private scheduleReconnect() {
    if (this.reconnectTimer !== null) return;
    // Exponential backoff: base * 2^attempt, capped, with ±25% jitter.
    const exp = this.baseReconnectDelayMs * Math.pow(2, this.reconnectAttempt);
    const capped = Math.min(exp, this.maxReconnectDelayMs);
    const jitter = capped * (0.75 + Math.random() * 0.5);
    this.reconnectAttempt = Math.min(this.reconnectAttempt + 1, 8);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      if (this.closed) return;
      // Replace the dead socket; handlers reattach in open().
      this.ws = this.open();
    }, jitter);
  }

  private flush() {
    if (this.ws.readyState !== WebSocket.OPEN) return;
    const pending = this.outbox;
    this.outbox = [];
    for (const p of pending) this.ws.send(JSON.stringify(p));
  }

  private send(payload: object) {
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(payload));
    // Still connecting: queue and flush on open rather than silently dropping.
    else if (this.ws.readyState === WebSocket.CONNECTING) this.outbox.push(payload);
  }

  /** `model` = the composer's CURRENT selection, carried on every message so the turn uses
   * exactly what the user sees — immune to set_model races across reconnects (a new cowork
   * session always reconnects once to adopt its scratch dir, which could drop a queued
   * set_model and leave the engine on a stale/resumed model; found 2026-07-04). */
  userMessage(text: string, attachments?: unknown[], model?: string) {
    this.send({
      type: "user_message",
      text,
      ...(model ? { model } : {}),
      ...(attachments?.length ? { attachments } : {}),
    });
  }

  approve(decision: string) {
    this.send({ type: "approval", decision });
  }

  // Reply to a `request_directory` prompt: grant a folder (with access level) or decline.
  respondDirectory(granted: boolean, path?: string, writable?: boolean) {
    this.send({ type: "directory_response", granted, ...(path ? { path } : {}), writable: !!writable });
  }

  // Reply to a `propose_plan` prompt: approve (choosing the execution mode) or reject with feedback.
  respondPlan(approved: boolean, mode?: string, feedback?: string) {
    this.send({
      type: "plan_response",
      approved,
      ...(mode ? { mode } : {}),
      ...(feedback ? { feedback } : {}),
    });
  }

  // Answer a live `ask_user` prompt (attended sessions; unattended ones answer via the Inbox).
  respondQuestion(answer: string) {
    this.send({ type: "question_response", answer });
  }

  interrupt() {
    this.send({ type: "interrupt" });
  }

  // Re-run a turn that ended in a provider error — no new user message; the server
  // guards on the history tail so a stray frame is a no-op.
  retry() {
    this.send({ type: "retry" });
  }

  setMode(mode: string) {
    this.send({ type: "set_mode", mode });
  }

  setModel(model: string) {
    this.send({ type: "set_model", model });
  }

  close() {
    // Intentional teardown: suppress auto-reconnect and detach handlers. This socket's
    // async `close` event may land AFTER the successor session's `open` (observed when
    // switching into an automation-run session), and a torn-down socket must not clobber
    // the new one's connected state or schedule a spurious reconnect.
    this.closed = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws.onopen = null;
    this.ws.onmessage = null;
    this.ws.onclose = null;
    this.ws.onerror = null;
    this.ws.close();
  }
}

// -- notify channels (钉钉/飞书/企微/webhook/邮件) ----------------------------
export interface NotifyChannelInfo {
  channel: string;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
}

export interface NotifyChannelsResponse {
  channels: NotifyChannelInfo[];
  meta: Record<string, { label: string; description: string }>;
}

export async function getNotifyChannels(): Promise<NotifyChannelsResponse> {
  const res = await fetch(`${httpBase()}/v1/notify/channels`);
  return res.json();
}

export async function getNotifyChannelConfig(
  channel: string,
): Promise<{ channel: string; config: Record<string, unknown> }> {
  const res = await fetch(`${httpBase()}/v1/notify/channels/${encodeURIComponent(channel)}`);
  return res.json();
}

export async function saveNotifyChannelConfig(
  channel: string,
  config: Record<string, unknown>,
  clearFields: string[] = [],
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/notify/channels/${encodeURIComponent(channel)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config, clear_fields: clearFields }),
  });
  return res.json();
}

export async function setNotifyChannelEnabled(
  channel: string,
  enabled: boolean,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/notify/channels/${encodeURIComponent(channel)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return res.json();
}

export async function deleteNotifyChannelConfig(
  channel: string,
): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/notify/channels/${encodeURIComponent(channel)}`, {
    method: "DELETE",
  });
  return res.json();
}

export async function testNotifyChannel(
  channel: string,
  config: Record<string, unknown>,
): Promise<{ ok: boolean; results?: Array<{ ok: boolean; channel: string; error: string | null }> }> {
  const res = await fetch(`${httpBase()}/v1/notify/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ channel, config }),
  });
  return res.json();
}

// -- digital humans (DHP bridge, 批次 C) -------------------------------------

export interface SelectOption {
  label: string;
  value: string | number | boolean;
}
export interface ConfigField {
  key: string;
  label: string;
  type: "string" | "text" | "number" | "boolean" | "url" | "email" | "select";
  required: boolean;
  description?: string;
  default?: unknown;
  placeholder?: string;
  options?: SelectOption[];
  secret?: boolean;
}
export interface DigitalHumanEntry {
  slug: string;
  name: string;
  version: string;
  author: string;
  description: string;
  type: string;
  category: string;
  tags: string[];
  icon: string;
  locale: string;
  min_app_version: string;
  updated_at: string;
  has_i18n: boolean;
  installed: boolean;
}
export interface DigitalHumanDetail {
  ok: boolean;
  error?: string;
  entry: DigitalHumanEntry;
  spec: {
    name: string;
    version: string;
    description: string;
    system_prompt: string;
    config_schema: ConfigField[];
    notify_channels: string[];
    has_schedule: boolean;
    slug: string;
    icon: string;
    spec_version: string;
    author: string;
    type: string;
    recommended_model: string | null;
  };
  requires_consent: {
    mcps: Array<{ id: string; reason: string; bundled: boolean }>;
    skills: Array<{ id: string; reason: string; bundled: boolean }>;
    plugins: Array<{ id: string; reason: string; bundled: boolean }>;
    commands: Array<{ id: string; reason: string; bundled: boolean }>;
    subagents: Array<{ id: string; reason: string; bundled: boolean }>;
    permissions: string[];
    browser_login: Array<{ url: string; label: string }>;
    has_schedule: boolean;
  };
}
export interface DigitalHumanInstance {
  id: string;
  slug: string;
  name: string;
  task_id: string;
  config: Record<string, unknown>;
  secret_keys: string[];
  spec_version: string;
  installed_at: number;
  updated_at: number;
  task: {
    id: string;
    title: string;
    schedule: string;
    schedule_raw: { kind: string; cron: string | null; fire_at: string | null; timezone: string };
    enabled: boolean;
    notify_channels: string[];
    notify_level: string;
    last_status: string | null;
    run_count: number;
  } | null;
}

export async function getDigitalHumans(
  category?: string,
): Promise<{ ok: boolean; humans: DigitalHumanEntry[]; categories: string[] }> {
  const q = category ? `?category=${encodeURIComponent(category)}` : "";
  const res = await fetch(`${httpBase()}/v1/digital-humans${q}`);
  return res.json();
}

export async function getDigitalHuman(slug: string): Promise<DigitalHumanDetail> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/${encodeURIComponent(slug)}`);
  return res.json();
}

export async function installDigitalHuman(
  slug: string,
  config: Record<string, unknown>,
): Promise<{ ok: boolean; error?: string; instance?: DigitalHumanInstance; task?: unknown }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/${encodeURIComponent(slug)}/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
  return res.json();
}

export async function getDigitalHumanInstances(): Promise<{ ok: boolean; instances: DigitalHumanInstance[] }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/instances`);
  return res.json();
}

export async function uninstallDigitalHuman(instanceId: string): Promise<{ ok: boolean }> {
  const res = await fetch(
    `${httpBase()}/v1/digital-humans/instances/${encodeURIComponent(instanceId)}`,
    { method: "DELETE" },
  );
  return res.json();
}

// 编辑已装实例：改 system_prompt / user_config / cron / notify / title / enabled。
export async function updateDigitalHumanInstance(
  instanceId: string,
  changes: {
    system_prompt?: string;
    user_config?: Record<string, unknown>;
    cron?: string;
    notify_channels?: string[];
    notify_level?: string;
    title?: string;
    enabled?: boolean;
  },
): Promise<{ ok: boolean; error?: string; instance?: DigitalHumanInstance }> {
  const res = await fetch(
    `${httpBase()}/v1/digital-humans/instances/${encodeURIComponent(instanceId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    },
  );
  return res.json();
}

// 源管理：列/增/改/删。
export interface DhpSource {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  is_default: boolean;
  source_type: string;
}

export async function getDhpSources(): Promise<{ ok: boolean; sources: DhpSource[] }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/sources`);
  return res.json();
}

export async function addDhpSource(
  name: string,
  url: string,
  sourceType = "dhp",
): Promise<{ ok: boolean; error?: string; source?: DhpSource }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, url, source_type: sourceType }),
  });
  return res.json();
}

export async function updateDhpSource(
  sourceId: string,
  changes: { name?: string; url?: string; enabled?: boolean },
): Promise<{ ok: boolean; error?: string; source?: DhpSource }> {
  const res = await fetch(
    `${httpBase()}/v1/digital-humans/sources/${encodeURIComponent(sourceId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    },
  );
  return res.json();
}

export async function removeDhpSource(sourceId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/digital-humans/sources/${encodeURIComponent(sourceId)}`,
    { method: "DELETE" },
  );
  return res.json();
}

// 依赖健康检查。
export interface McpHealthItem {
  name: string;
  reason: string;
  configured: boolean;
  status: string;
  tool_count: number;
}
export async function getDhpMcpHealth(
  slug: string,
): Promise<{ ok: boolean; error?: string; items: McpHealthItem[] }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/${encodeURIComponent(slug)}/mcp-health`);
  return res.json();
}

export interface SkillHealthItem {
  id: string;
  reason: string;
  installed: boolean;
}
export async function getDhpSkillsHealth(
  slug: string,
): Promise<{ ok: boolean; error?: string; items: SkillHealthItem[] }> {
  const res = await fetch(`${httpBase()}/v1/digital-humans/${encodeURIComponent(slug)}/skills-health`);
  return res.json();
}

// 升级检查。
export async function getDhpUpgradeCheck(
  instanceId: string,
): Promise<{
  ok: boolean;
  error?: string;
  installed_version: string;
  latest_version: string;
  up_to_date: boolean;
}> {
  const res = await fetch(
    `${httpBase()}/v1/digital-humans/instances/${encodeURIComponent(instanceId)}/upgrade-check`,
  );
  return res.json();
}
