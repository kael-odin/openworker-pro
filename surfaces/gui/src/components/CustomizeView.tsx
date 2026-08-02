// The Customize surface — one entry point to manage every installed extension kind.
// Reached from the sidebar's "Customize" nav row (below Automations).
//
// Layout: search box on top, a single horizontal row of 7 category tabs beneath it
// (Plugins / MCPs / Skills / Subagents / Rules / Commands / Hooks), and the selected
// category's installed/configured content in a panel below the tab row. Search filters
// the active category's items. "Browse Marketplace" opens CustomizeMarketplace for
// cross-source install.
//
// Readiness varies (see docs/EXTENSIONS-ROADMAP.md):
//   - MCPs (McpRow reused), Skills (getSkills, E1 install/uninstall), Subagents (getPersonas): live.
//   - Plugins / Rules / Commands / Hooks: "coming soon" placeholders until E2-E5.
import { useEffect, useRef, useState } from "react";
import {
  addHook,
  addRule,
  checkPluginUpdates,
  deleteMcpServer,
  deletePersona,
  getBrowserLogins,
  getCommands,
  getCommand,
  getHooks,
  exportBrowserLogins,
  getMcpServers,
  getPersonas,
  getPlugins,
  getRules,
  getSkills,
  importBrowserLogins,
  patchMcpServer,
  removeBrowserLogin,
  removeHook,
  removeRule,
  setPersonaEnabled,
  uninstallPlugin,
  uninstallSkill,
  updateHook,
  updatePlugin,
  updateRule,
  type BrowserLogin,
  type Command,
  type Hook,
  type HookEvent,
  type InstalledPlugin,
  type McpServer,
  type Persona,
  type Rule,
  type RuleAction,
  type Skill,
} from "../api";
import { TOOL_HOOK_EVENTS } from "../api";
import { Icon } from "./Icon";
import { PanelHead } from "./IntegrationsView";
import { McpRow } from "./ManageTabs";
import { CustomizeMarketplace } from "./CustomizeMarketplace";
import { BrowserLoginModal } from "./BrowserLoginModal";
import { loginExpiryChip } from "./loginExpiryChip";
import { useT } from "../i18n/I18nProvider";

// The §28 page shell: full-bleed main, centered ≤4xl column — same as Connectors/Activity.
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex-1 min-w-0 flex bg-paper">
      <div className="flex-1 min-w-0 overflow-y-auto hairline-scroll">
        <div className="max-w-4xl mx-auto px-7 py-6">{children}</div>
      </div>
    </main>
  );
}

const CARD = "rounded-xl2 border border-line bg-panel";
const SEARCH_INPUT =
  "flex-1 px-3 py-2 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent";

// The 8 categories, in the user's stated order. Each tab is a horizontal button with
// icon + title + count badge; clicking it swaps the panel below to that category.
type Cat = "plugins" | "mcp" | "skills" | "subagents" | "rules" | "commands" | "hooks" | "logins";

const CATS: { key: Cat; icon: string; titleKey: string; descKey: string }[] = [
  { key: "plugins", icon: "puzzle", titleKey: "customize.plugins_title", descKey: "customize.plugins_desc" },
  { key: "mcp", icon: "code", titleKey: "customize.mcp_title", descKey: "customize.mcp_desc" },
  { key: "skills", icon: "file", titleKey: "customize.skills_title", descKey: "customize.skills_desc" },
  { key: "subagents", icon: "bot", titleKey: "customize.subagents_title", descKey: "customize.subagents_desc" },
  { key: "rules", icon: "listChecks", titleKey: "customize.rules_title", descKey: "customize.rules_desc" },
  { key: "commands", icon: "terminal", titleKey: "customize.commands_title", descKey: "customize.commands_desc" },
  { key: "hooks", icon: "sliders", titleKey: "customize.hooks_title", descKey: "customize.hooks_desc" },
  { key: "logins", icon: "key", titleKey: "customize.logins_title", descKey: "customize.logins_desc" },
];

export function CustomizeView() {
  const { t } = useT();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState<Cat>("skills");
  const [showMarket, setShowMarket] = useState(false);

  const [mcps, setMcps] = useState<McpServer[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [hooks, setHooks] = useState<Hook[]>([]);
  const [commands, setCommands] = useState<Command[]>([]);
  const [plugins, setPlugins] = useState<InstalledPlugin[]>([]);
  const [logins, setLogins] = useState<BrowserLogin[]>([]);

  const refresh = () => {
    getMcpServers().then(setMcps).catch(() => setMcps([]));
    getSkills().then(setSkills).catch(() => setSkills([]));
    getPersonas().then(setPersonas).catch(() => setPersonas([]));
    getRules().then(setRules).catch(() => setRules([]));
    getHooks().then(setHooks).catch(() => setHooks([]));
    getCommands().then(setCommands).catch(() => setCommands([]));
    getPlugins().then(setPlugins).catch(() => setPlugins([]));
    getBrowserLogins().then(setLogins).catch(() => setLogins([]));
  };
  useEffect(() => {
    refresh();
  }, []);

  // Count per category for the tab badges.
  const counts: Record<Cat, number> = {
    plugins: plugins.length,
    mcp: mcps.length,
    skills: skills.length,
    subagents: personas.length,
    rules: rules.length,
    commands: commands.length,
    hooks: hooks.length,
    logins: logins.filter((l) => l.has_state).length,
  };

  // Search filters the *active* category's items (cross-category filtering isn't useful
  // when only one panel is visible at a time).
  const q = query.trim().toLowerCase();
  const match = (s: string) => !q || s.toLowerCase().includes(q);
  const mcpsF = mcps.filter((m) => match(m.name));
  const skillsF = skills.filter((s) => match(s.name + " " + s.description));
  const personasF = personas.filter((p) => match(p.id + " " + p.name + " " + p.tagline));
  const rulesF = rules.filter((r) => match(r.pattern + " " + r.action + " " + r.reason));
  const commandsF = commands.filter((c) => match(c.name + " " + c.description));
  const hooksF = hooks.filter((h) => match(h.name + " " + h.event + " " + h.match + " " + h.match_tool + " " + h.command));
  const pluginsF = plugins.filter((p) => match(p.name + " " + p.description));
  const loginsF = logins.filter((l) => match(l.label + " " + l.url));

  return (
    <Shell>
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <PanelHead title={t("customize.title")} sub={t("customize.sub")} />
        </div>
        <button
          className="text-[12.5px] px-3 py-1.5 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0 flex items-center gap-1.5"
          onClick={() => setShowMarket(true)}
        >
          <Icon name="puzzle" size={14} /> {t("customize.browse_marketplace")}
        </button>
      </div>

      {/* Search */}
      <div className="flex gap-2 mt-4 mb-3">
        <input
          className={SEARCH_INPUT}
          placeholder={t("customize.search_placeholder")}
          value={query}
          spellCheck={false}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {/* 8 category tabs — a single horizontal row */}
      <div className="flex gap-1 mb-4 overflow-x-auto hairline-scroll pb-1">
        {CATS.map((c) => {
          const isActive = active === c.key;
          return (
            <button
              key={c.key}
              className={
                "flex items-center gap-1.5 px-3 py-2 rounded-lg text-[12.5px] whitespace-nowrap transition-colors shrink-0 " +
                (isActive
                  ? "bg-accent text-white"
                  : "bg-panel border border-line text-muted hover:text-ink hover:border-lineStrong")
              }
              onClick={() => setActive(c.key)}
            >
              <Icon name={c.icon as any} size={14} className={isActive ? "text-white" : "text-muted"} />
              <span>{t(c.titleKey)}</span>
              <span
                className={
                  "text-[10.5px] px-1.5 py-0.5 rounded-full shrink-0 " +
                  (isActive
                    ? "bg-white/20 text-white"
                    : "bg-paper border border-line text-faint")
                }
              >
                {counts[c.key]}
              </span>
            </button>
          );
        })}
      </div>

      {/* Active category panel */}
      <div className={CARD + " p-4"}>
        {active === "plugins" && (
          <PluginsPanel plugins={pluginsF} t={t} onRefresh={refresh} />
        )}

        {active === "mcp" && (
          mcpsF.length === 0 ? (
            <EmptyKind kind={t("customize.mcp_title")} t={t} />
          ) : (
            <div className="space-y-2">
              {mcpsF.map((m) => (
                <McpRow
                  key={m.name}
                  server={m}
                  onToggle={async () => {
                    await patchMcpServer(m.name, { enabled: !m.enabled });
                    refresh();
                  }}
                  onRemove={async () => {
                    await deleteMcpServer(m.name);
                    refresh();
                  }}
                  onRefresh={refresh}
                />
              ))}
            </div>
          )
        )}

        {active === "skills" && (
          skillsF.length === 0 ? (
            <EmptyKind kind={t("customize.skills_title")} t={t} />
          ) : (
            <div className="divide-y divide-line">
              {skillsF.map((s) => (
                <div key={s.name} className="flex items-center gap-3 py-2.5">
                  <Icon name="file" size={14} className="text-faint shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-medium text-ink">{s.name}</div>
                    {s.description && (
                      <div className="text-[11.5px] text-faint truncate">{s.description}</div>
                    )}
                  </div>
                  <button
                    className="text-faint hover:text-danger p-1"
                    title={t("common.delete_aria", { title: s.name })}
                    onClick={async () => {
                      await uninstallSkill(s.name);
                      refresh();
                    }}
                  >
                    <Icon name="trash" size={14} />
                  </button>
                </div>
              ))}
            </div>
          )
        )}

        {active === "subagents" && (
          personasF.length === 0 ? (
            <EmptyKind kind={t("customize.subagents_title")} t={t} />
          ) : (
            <div className="divide-y divide-line">
              {personasF.map((p) => (
                <div key={p.id} className="flex items-center gap-3 py-2.5">
                  <Icon name={(p.icon || "bot") as any} size={14} className="text-muted shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] font-medium text-ink">{p.name}</span>
                      {p.enabled ? (
                        <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-accentSoft text-accent">
                          {t("digital.enabled")}
                        </span>
                      ) : (
                        <span className="text-[10.5px] px-1.5 py-0.5 rounded-full border border-line text-faint">
                          {t("digital.disabled")}
                        </span>
                      )}
                    </div>
                    {p.tagline && <div className="text-[11.5px] text-faint truncate">{p.tagline}</div>}
                  </div>
                  <label className="switch shrink-0" title="Enable / disable">
                    <input
                      type="checkbox"
                      checked={p.enabled}
                      onChange={async () => {
                        await setPersonaEnabled(p.id, !p.enabled);
                        refresh();
                      }}
                    />
                    <span className="slider" />
                  </label>
                  {!p.builtin && (
                    <button
                      className="text-faint hover:text-danger p-1"
                      title={t("common.delete_aria", { title: p.name })}
                      onClick={async () => {
                        await deletePersona(p.id);
                        refresh();
                      }}
                    >
                      <Icon name="trash" size={14} />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )
        )}

        {active === "rules" && (
          <RulesPanel rules={rulesF} t={t} onRefresh={refresh} />
        )}
        {active === "commands" && (
          <CommandsPanel commands={commandsF} t={t} onRefresh={refresh} />
        )}
        {active === "hooks" && <HooksPanel hooks={hooksF} t={t} onRefresh={refresh} />}
        {active === "logins" && (
          <LoginsPanel logins={loginsF} t={t} onRefresh={refresh} />
        )}
      </div>

      {showMarket && (
        <CustomizeMarketplace
          onClose={() => {
            setShowMarket(false);
            // The marketplace may have installed/uninstalled skills — refresh so the
            // Customize list reflects the new state.
            refresh();
          }}
        />
      )}
    </Shell>
  );
}

// Empty-state for a category with no installed items.
function EmptyKind({
  kind,
  t,
}: {
  kind: string;
  t: (key: string, params?: Record<string, string | number>) => string;
}) {
  return (
    <div className="text-[12.5px] text-faint py-8 text-center">
      {t("customize.empty_installed", { kind })}
    </div>
  );
}

// Shared t() signature passed to sub-panels.
export type TFn = (key: string, params?: Record<string, string | number>) => string;

const INPUT =
  "px-2.5 py-1.5 rounded-md border border-line bg-paper text-[12.5px] text-ink outline-none focus:border-accent min-w-0";
const BTN_PRIMARY =
  "text-[12px] px-2.5 py-1.5 rounded-md bg-accent text-white hover:opacity-90";

// Rules panel — glob pattern + allow/deny/ask action + reason. Add inline, delete per row,
// toggle action via dropdown. Rules are the user-facing permission layer (E2).
function RulesPanel({
  rules,
  t,
  onRefresh,
}: {
  rules: Rule[];
  t: TFn;
  onRefresh: () => void;
}) {
  const [pattern, setPattern] = useState("");
  const [action, setAction] = useState<RuleAction>("ask");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    if (!pattern.trim()) return;
    setBusy(true);
    await addRule(pattern.trim(), action, reason.trim());
    setPattern("");
    setReason("");
    setAction("ask");
    setBusy(false);
    onRefresh();
  };

  return (
    <div>
      {/* Add-form row */}
      <div className="flex flex-wrap items-center gap-2 mb-3 pb-3 border-b border-line">
        <input
          className={INPUT + " flex-1"}
          placeholder={t("customize.rule_pattern_ph")}
          value={pattern}
          spellCheck={false}
          onChange={(e) => setPattern(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && add()}
        />
        <select
          className={INPUT}
          value={action}
          onChange={(e) => setAction(e.target.value as RuleAction)}
        >
          <option value="allow">allow</option>
          <option value="deny">deny</option>
          <option value="ask">ask</option>
        </select>
        <input
          className={INPUT + " flex-1"}
          placeholder={t("customize.rule_reason_ph")}
          value={reason}
          spellCheck={false}
          onChange={(e) => setReason(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && add()}
        />
        <button className={BTN_PRIMARY} disabled={busy || !pattern.trim()} onClick={add}>
          {t("customize.add_rule")}
        </button>
      </div>

      {rules.length === 0 ? (
        <div className="text-[12px] text-faint py-6 text-center leading-relaxed">
          {t("customize.rule_empty")}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {rules.map((r) => (
            <div key={r.id} className="flex items-center gap-2 py-2">
              <code className="text-[12px] text-ink font-mono flex-1 min-w-0 truncate">
                {r.pattern}
              </code>
              <select
                className={INPUT + " w-auto"}
                value={r.action}
                onChange={async (e) => {
                  await updateRule(r.id, { action: e.target.value as RuleAction });
                  onRefresh();
                }}
              >
                <option value="allow">allow</option>
                <option value="deny">deny</option>
                <option value="ask">ask</option>
              </select>
              {r.reason && (
                <span className="text-[11.5px] text-faint truncate max-w-[30%]">{r.reason}</span>
              )}
              <button
                className="text-faint hover:text-danger p-1 shrink-0"
                title={t("common.delete_aria", { title: r.pattern })}
                onClick={async () => {
                  await removeRule(r.id);
                  onRefresh();
                }}
              >
                <Icon name="trash" size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Hooks panel — name + event + match glob + command. Add inline, toggle enabled,
// delete per row. Run-level events (pre_run/post_run) fire around scheduled runs;
// tool-level events (pre_tool/post_tool/on_message) fire per tool call / per message
// (modeled on Claude Code's PreToolUse/PostToolUse — pre_tool can short-circuit a
// call). Tool events also take a match_tool glob (which tool name triggers them).
function HooksPanel({
  hooks,
  t,
  onRefresh,
}: {
  hooks: Hook[];
  t: TFn;
  onRefresh: () => void;
}) {
  const [name, setName] = useState("");
  const [event, setEvent] = useState<HookEvent>("post_run");
  const [match, setMatch] = useState("*");
  const [matchTool, setMatchTool] = useState("*");
  const [command, setCommand] = useState("");
  const [busy, setBusy] = useState(false);

  // Whether the currently-selected event is a tool-level event (shows match_tool).
  const isToolEvent = TOOL_HOOK_EVENTS.includes(event);

  const add = async () => {
    if (!name.trim() || !command.trim()) return;
    setBusy(true);
    await addHook(
      name.trim(),
      event,
      command.trim(),
      match.trim() || "*",
      isToolEvent ? matchTool.trim() || "*" : "*",
    );
    setName("");
    setCommand("");
    setMatch("*");
    setMatchTool("*");
    setEvent("post_run");
    setBusy(false);
    onRefresh();
  };

  // Label for an event value, used in both the select and the row badge.
  const eventLabel = (ev: HookEvent) => {
    switch (ev) {
      case "pre_run": return t("customize.hook_event_pre_run");
      case "post_run": return t("customize.hook_event_post_run");
      case "pre_tool": return t("customize.hook_event_pre_tool");
      case "post_tool": return t("customize.hook_event_post_tool");
      case "on_message": return t("customize.hook_event_on_message");
    }
  };

  return (
    <div>
      {/* Add-form */}
      <div className="grid grid-cols-2 gap-2 mb-3 pb-3 border-b border-line">
        <input
          className={INPUT}
          placeholder={t("customize.hook_name_ph")}
          value={name}
          spellCheck={false}
          onChange={(e) => setName(e.target.value)}
        />
        <select
          className={INPUT}
          value={event}
          onChange={(e) => setEvent(e.target.value as HookEvent)}
        >
          <optgroup label={t("customize.hook_eventgroup_run")}>
            <option value="pre_run">{t("customize.hook_event_pre_run")}</option>
            <option value="post_run">{t("customize.hook_event_post_run")}</option>
          </optgroup>
          <optgroup label={t("customize.hook_eventgroup_tool")}>
            <option value="pre_tool">{t("customize.hook_event_pre_tool")}</option>
            <option value="post_tool">{t("customize.hook_event_post_tool")}</option>
            <option value="on_message">{t("customize.hook_event_on_message")}</option>
          </optgroup>
        </select>
        <input
          className={INPUT}
          placeholder={t("customize.hook_match_ph")}
          value={match}
          spellCheck={false}
          onChange={(e) => setMatch(e.target.value)}
        />
        <input
          className={INPUT}
          placeholder={isToolEvent ? t("customize.hook_match_tool_ph") : t("customize.hook_command_ph")}
          value={isToolEvent ? matchTool : command}
          spellCheck={false}
          onChange={(e) => (isToolEvent ? setMatchTool(e.target.value) : setCommand(e.target.value))}
        />
        {isToolEvent && (
          <input
            className={INPUT}
            placeholder={t("customize.hook_command_ph")}
            value={command}
            spellCheck={false}
            onChange={(e) => setCommand(e.target.value)}
          />
        )}
        <div className="col-span-2 flex justify-end">
          <button
            className={BTN_PRIMARY}
            disabled={busy || !name.trim() || !command.trim()}
            onClick={add}
          >
            {t("customize.add_hook")}
          </button>
        </div>
      </div>

      {hooks.length === 0 ? (
        <div className="text-[12px] text-faint py-6 text-center leading-relaxed">
          {t("customize.hook_empty")}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {hooks.map((h) => {
            const hIsTool = TOOL_HOOK_EVENTS.includes(h.event);
            return (
            <div key={h.id} className="py-2.5">
              <div className="flex items-center gap-2">
                <span className="text-[13px] font-medium text-ink flex-1 min-w-0 truncate">
                  {h.name}
                </span>
                <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-accentSoft text-accent shrink-0">
                  {eventLabel(h.event)}
                </span>
                <label className="switch shrink-0" title="Enable / disable">
                  <input
                    type="checkbox"
                    checked={h.enabled}
                    onChange={async () => {
                      await updateHook(h.id, { enabled: !h.enabled });
                      onRefresh();
                    }}
                  />
                  <span className="slider" />
                </label>
                <button
                  className="text-faint hover:text-danger p-1 shrink-0"
                  title={t("common.delete_aria", { title: h.name })}
                  onClick={async () => {
                    await removeHook(h.id);
                    onRefresh();
                  }}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
              <div className="flex items-center gap-2 mt-1 text-[11.5px] text-faint flex-wrap">
                <span className="font-mono">{h.match}</span>
                {hIsTool && h.match_tool && h.match_tool !== "*" && (
                  <>
                    <span>·</span>
                    <span className="font-mono">{h.match_tool}</span>
                  </>
                )}
                <span>›</span>
                <code className="font-mono truncate">{h.command}</code>
              </div>
            </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Commands panel — read-only list of installed slash commands (E3). Each row shows the
// /name, description, and allowed-tools chips; "view template" opens a modal with the full
// prompt_template. Commands are hand-authored (state_dir()/commands/<name>/COMMAND.md) or
// installed via E4 plugin packaging — no create/edit UI in this batch.
function CommandsPanel({
  commands,
  t,
}: {
  commands: Command[];
  t: TFn;
  onRefresh: () => void;
}) {
  const [viewing, setViewing] = useState<Command | null>(null);

  return (
    <div>
      {commands.length === 0 ? (
        <div className="text-[12px] text-faint py-6 text-center leading-relaxed">
          {t("customize.commands_empty")}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {commands.map((c) => (
            <div key={c.name} className="flex items-center gap-2.5 py-2.5">
              <code className="text-[12.5px] text-ink font-mono shrink-0">/{c.name}</code>
              <span className="text-[12px] text-muted flex-1 min-w-0 truncate">{c.description}</span>
              {c.allowed_tools && c.allowed_tools.length > 0 && (
                <div className="hidden sm:flex items-center gap-1 shrink-0">
                  {c.allowed_tools.slice(0, 3).map((tool) => (
                    <span
                      key={tool}
                      className="text-[10.5px] px-1.5 py-0.5 rounded bg-paper border border-line text-faint font-mono"
                    >
                      {tool}
                    </span>
                  ))}
                  {c.allowed_tools.length > 3 && (
                    <span className="text-[10.5px] text-faint">+{c.allowed_tools.length - 3}</span>
                  )}
                </div>
              )}
              <button
                className="text-faint hover:text-accent p-1 shrink-0"
                title={t("customize.commands_view_template")}
                onClick={async () => {
                  // The list item only carries name+description; fetch the full template.
                  try {
                    const full = await getCommand(c.name);
                    setViewing(full ?? c);
                  } catch {
                    setViewing(c);
                  }
                }}
              >
                <Icon name="file" size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Template viewer modal */}
      {viewing && (
        <div
          className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6"
          onClick={() => setViewing(null)}
        >
          <div
            className="bg-panel rounded-xl border border-line shadow-2xl max-w-2xl w-full max-h-[80vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 px-4 py-3 border-b border-line">
              <code className="text-[13px] font-mono text-ink">/{viewing.name}</code>
              <span className="text-[12px] text-faint truncate">{viewing.description}</span>
              <button
                className="ml-auto text-faint hover:text-ink p-1"
                onClick={() => setViewing(null)}
                aria-label={t("common.close")}
              >
                <Icon name="x" size={15} />
              </button>
            </div>
            <pre className="flex-1 overflow-y-auto px-4 py-3 text-[12.5px] text-muted font-mono whitespace-pre-wrap hairline-scroll">
              {viewing.prompt_template || t("customize.commands_empty")}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

// Plugins panel (E4): list of installed plugins from state_dir()/plugins/<name>/. Each row
// shows name + version + component chips (skills/commands/mcps counts) + an update button
// (highlighted when an update is available) + uninstall. Plugins are installed from the
// marketplace (CustomizeMarketplace ▸ Plugins) — no create UI here.
function PluginsPanel({
  plugins,
  t,
  onRefresh,
}: {
  plugins: InstalledPlugin[];
  t: TFn;
  onRefresh: () => void;
}) {
  const [updates, setUpdates] = useState<Record<string, boolean>>({});
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState<string>("");

  // Check for updates across all installed plugins (compares recorded sha vs marketplace sha).
  const checkUpdates = async () => {
    setChecking(true);
    try {
      const r = await checkPluginUpdates();
      const map: Record<string, boolean> = {};
      (r.items ?? []).forEach((it) => {
        map[it.name] = !it.up_to_date;
      });
      setUpdates(map);
    } catch {
      /* ignore — non-fatal */
    }
    setChecking(false);
  };

  const uninstall = async (name: string) => {
    setBusy(name);
    await uninstallPlugin(name);
    setBusy("");
    onRefresh();
  };

  const update = async (name: string) => {
    setBusy(name);
    await updatePlugin(name);
    setBusy("");
    // Re-check updates after applying one.
    checkUpdates();
    onRefresh();
  };

  return (
    <div>
      {/* Toolbar: check-for-updates button */}
      {plugins.length > 0 && (
        <div className="flex items-center justify-between mb-3 pb-3 border-b border-line">
          <span className="text-[11.5px] text-faint">
            {checking
              ? t("customize.plugins_checking_updates")
              : Object.values(updates).some(Boolean)
                ? t("customize.plugins_update_available")
                : Object.keys(updates).length > 0
                  ? t("customize.plugins_no_updates")
                  : ""}
          </span>
          <button
            className="text-[12px] px-2.5 py-1 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent disabled:opacity-50"
            disabled={checking}
            onClick={checkUpdates}
          >
            {t("customize.plugins_update")}
          </button>
        </div>
      )}

      {plugins.length === 0 ? (
        <div className="text-[12px] text-faint py-8 text-center leading-relaxed">
          {t("customize.plugins_empty")}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {plugins.map((p) => {
            const hasUpdate = updates[p.name] === true;
            const isBusy = busy === p.name;
            return (
              <div key={p.name} className="py-2.5">
                <div className="flex items-center gap-2.5">
                  <Icon name="puzzle" size={14} className="text-muted shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] font-medium text-ink">{p.name}</span>
                      {p.version && (
                        <span className="text-[10.5px] text-faint">
                          {t("customize.plugins_version")}: {p.version}
                        </span>
                      )}
                      {!p.present && (
                        <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-dangerSoft text-danger">
                          {t("root.missing")}
                        </span>
                      )}
                    </div>
                    {p.description && (
                      <div className="text-[11.5px] text-faint truncate">{p.description}</div>
                    )}
                  </div>
                  {hasUpdate && (
                    <button
                      className="text-[11.5px] px-2 py-1 rounded-md bg-accent text-white hover:opacity-90 disabled:opacity-50 shrink-0"
                      disabled={isBusy}
                      onClick={() => update(p.name)}
                    >
                      {isBusy ? "…" : t("customize.plugins_update")}
                    </button>
                  )}
                  <button
                    className="text-faint hover:text-danger p-1 shrink-0 disabled:opacity-50"
                    title={t("common.delete_aria", { title: p.name })}
                    disabled={isBusy}
                    onClick={() => uninstall(p.name)}
                  >
                    <Icon name="trash" size={14} />
                  </button>
                </div>
                {/* Component chips: skills/commands/mcps counts */}
                {(p.components.skills.length > 0 ||
                  p.components.commands.length > 0 ||
                  p.components.mcps.length > 0) && (
                  <div className="flex items-center gap-1.5 mt-1.5 ml-[22px]">
                    <span className="text-[10px] text-faint">{t("customize.plugins_components")}:</span>
                    {p.components.skills.length > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-paper border border-line text-faint">
                        {p.components.skills.length} skills
                      </span>
                    )}
                    {p.components.commands.length > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-paper border border-line text-faint">
                        {p.components.commands.length} commands
                      </span>
                    )}
                    {p.components.mcps.length > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-paper border border-line text-faint">
                        {p.components.mcps.length} mcp
                      </span>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Logins panel (E5): persisted browser login sessions. Each row shows the site label +
// url + a logged-in / not-logged-in chip + delete. "Add login" opens BrowserLoginModal,
// which captures the session either via a headed Playwright window (preferred) or pasted
// cookie JSON (fallback when Playwright isn't installed). browser_open_url auto-loads the
// matching session when the agent visits the site later.
function LoginsPanel({
  logins,
  t,
  onRefresh,
}: {
  logins: BrowserLogin[];
  t: TFn;
  onRefresh: () => void;
}) {
  // false = closed; BrowserLogin = open in re-login mode for that entry.
  const [adding, setAdding] = useState<boolean | BrowserLogin>(false);
  const [busy, setBusy] = useState<string>("");
  const [importMsg, setImportMsg] = useState("");
  const fileRef = useRef<HTMLInputElement | null>(null);

  const remove = async (id: string) => {
    setBusy(id);
    await removeBrowserLogin(id);
    setBusy("");
    onRefresh();
  };

  const doExport = async () => {
    setBusy("export");
    try {
      const data = await exportBrowserLogins();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "openworker-logins.json";
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      /* ignore */
    }
    setBusy("");
  };

  const doImport = async (file: File) => {
    setBusy("import");
    setImportMsg("");
    try {
      const text = await file.text();
      const r = await importBrowserLogins(text);
      if (r.ok) {
        setImportMsg(t("customize.logins_imported", { count: r.imported ?? 0 }));
        onRefresh();
      } else {
        setImportMsg(t("customize.logins_import_failed", { error: r.error ?? "" }));
      }
    } catch (e: any) {
      setImportMsg(t("customize.logins_import_failed", { error: String(e?.message ?? e) }));
    }
    setBusy("");
  };

  return (
    <div>
      {/* Toolbar: add-login + export/import */}
      <div className="flex items-center justify-between mb-3 pb-3 border-b border-line">
        <span className="text-[11.5px] text-faint">{t("customize.logins_desc")}</span>
        <div className="flex items-center gap-1.5">
          <button
            className="text-[11.5px] px-2 py-1 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent disabled:opacity-50"
            disabled={!!busy}
            onClick={doExport}
            title={t("customize.logins_export")}
          >
            {t("customize.logins_export")}
          </button>
          <button
            className="text-[11.5px] px-2 py-1 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent disabled:opacity-50"
            disabled={!!busy}
            onClick={() => fileRef.current?.click()}
            title={t("customize.logins_import")}
          >
            {t("customize.logins_import")}
          </button>
          <button className={BTN_PRIMARY} onClick={() => setAdding(true)}>
            + {t("customize.logins_add")}
          </button>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) doImport(f);
            e.target.value = "";
          }}
        />
      </div>
      {importMsg && (
        <div className="text-[11.5px] text-faint mb-2">{importMsg}</div>
      )}

      {logins.length === 0 ? (
        <div className="text-[12px] text-faint py-8 text-center leading-relaxed">
          {t("customize.logins_empty")}
        </div>
      ) : (
        <div className="divide-y divide-line">
          {logins.map((l) => {
            const isBusy = busy === l.id;
            return (
              <div key={l.id} className="flex items-center gap-2.5 py-2.5">
                <Icon name="key" size={14} className="text-muted shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-ink">{l.label || l.url}</span>
                    {l.has_state ? (
                      <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-accentSoft text-accent">
                        {t("customize.logins_has_state")}
                      </span>
                    ) : (
                      <span className="text-[10.5px] px-1.5 py-0.5 rounded-full border border-line text-faint">
                        {t("customize.logins_no_state")}
                      </span>
                    )}
                    {(() => {
                      const chip = loginExpiryChip(l.expiry, t);
                      return chip ? (
                        <span className={"text-[10px] px-1.5 py-0.5 rounded-full " + chip.cls}>
                          {chip.text}
                        </span>
                      ) : null;
                    })()}
                  </div>
                  <div className="text-[11.5px] text-faint truncate">{l.url}</div>
                </div>
                <button
                  className="text-[11.5px] px-2 py-1 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0 disabled:opacity-50"
                  disabled={isBusy}
                  onClick={() => setAdding(l)}
                >
                  {l.has_state ? t("customize.logins_relogin") : t("customize.logins_add")}
                </button>
                <button
                  className="text-faint hover:text-danger p-1 shrink-0 disabled:opacity-50"
                  title={t("common.delete_aria", { title: l.label || l.url })}
                  disabled={isBusy}
                  onClick={() => remove(l.id)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {adding && (
        <BrowserLoginModal
          existing={adding === true ? null : (adding as BrowserLogin)}
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            onRefresh();
          }}
        />
      )}
    </div>
  );
}
