// DhEditPanel — 已装数字人实例的编辑面板（批次 D2），深度复刻 halo AppConfigPanel 的 11 区块。
// 入口：DigitalHumansSection 实例列表点「编辑」→进入此面板，带 instance + 回调。
// 保存：每个区块改动后调 updateDigitalHumanInstance(instance.id, changes)。
import { useEffect, useState } from "react";
import {
  getDigitalHuman,
  updateDigitalHumanInstance,
  getDhpCommandsHealth,
  getDhpLoginsHealth,
  getDhpMcpHealth,
  getDhpPluginsHealth,
  getDhpSkillsHealth,
  getDhpSubagentsHealth,
  getDhpUpgradeCheck,
  getPluginSources,
  installPlugin,
  type DepHealthItem,
  type ConfigField,
  type DigitalHumanInstance,
  type DigitalHumanDetail,
  type LoginHealthItem,
  type McpHealthItem,
  type SkillHealthItem,
} from "../../api";
import { useT } from "../../i18n/I18nProvider";
import { GRP, GRP_H } from "../connectors/ui";
import { Icon } from "../Icon";
import { BrowserLoginModal } from "../BrowserLoginModal";
import { loginExpiryChip } from "../loginExpiryChip";
import { SchedulePicker } from "./SchedulePicker";
import { SystemPromptEditor } from "./SystemPromptEditor";
import { DhConfigBlock } from "./blocks/DhConfigBlock";
import { DhNotifyBlock } from "./blocks/DhNotifyBlock";

export function DhEditPanel({
  instance,
  onBack,
}: {
  instance: DigitalHumanInstance;
  onBack: () => void;
}) {
  const { t } = useT();
  const [detail, setDetail] = useState<DigitalHumanDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [mcpHealth, setMcpHealth] = useState<McpHealthItem[]>([]);
  const [skillsHealth, setSkillsHealth] = useState<SkillHealthItem[]>([]);
  const [pluginsHealth, setPluginsHealth] = useState<DepHealthItem[]>([]);
  const [commandsHealth, setCommandsHealth] = useState<DepHealthItem[]>([]);
  const [subagentsHealth, setSubagentsHealth] = useState<DepHealthItem[]>([]);
  const [loginsHealth, setLoginsHealth] = useState<LoginHealthItem[]>([]);
  const [loginTarget, setLoginTarget] = useState<{ url: string; label: string } | null>(null);
  const [installingDep, setInstallingDep] = useState<string>("");
  const [upgrade, setUpgrade] = useState<{ installed_version: string; latest_version: string; up_to_date: boolean } | null>(null);

  // local mutable state for the editor (patched on save)
  const [cron, setCron] = useState(instance.task?.schedule_raw?.cron || "0 0 * * *");
  const [systemPrompt, setSystemPrompt] = useState(detail?.spec?.system_prompt || "");
  const [userConfig, setUserConfig] = useState<Record<string, unknown>>(instance.config || {});
  const [notifyChannels, setNotifyChannels] = useState<string[]>(instance.task?.notify_channels || []);
  const [notifyLevel, setNotifyLevel] = useState(instance.task?.notify_level || "important");
  const [enabled, setEnabled] = useState(instance.task?.enabled ?? true);
  // Local editable copy of config_schema. Seeded from the spec (which the backend already
  // resolves through any instance-level override), patched in place by the form editor, and
  // persisted via PATCH { config_schema } → instance.config_schema_override.
  const [schema, setSchema] = useState<ConfigField[]>(detail?.spec?.config_schema || []);

  useEffect(() => {
    setLoading(true);
    getDigitalHuman(instance.slug)
      .then((d) => {
        if (d.ok) {
          setDetail(d);
          setSystemPrompt(d.spec.system_prompt);
          setSchema(d.spec.config_schema);
          // seed userConfig with defaults from spec for any missing keys
          const seeded = { ...instance.config };
          for (const f of d.spec.config_schema) {
            if (!(f.key in seeded) && f.default !== undefined) seeded[f.key] = f.default;
          }
          setUserConfig(seeded);
        }
      })
      .finally(() => setLoading(false));
    getDhpMcpHealth(instance.slug).then((r) => r.ok && setMcpHealth(r.items));
    getDhpSkillsHealth(instance.slug).then((r) => r.ok && setSkillsHealth(r.items));
    getDhpPluginsHealth(instance.slug).then((r) => r.ok && setPluginsHealth(r.items ?? []));
    getDhpCommandsHealth(instance.slug).then((r) => r.ok && setCommandsHealth(r.items ?? []));
    getDhpSubagentsHealth(instance.slug).then((r) => r.ok && setSubagentsHealth(r.items ?? []));
    getDhpLoginsHealth(instance.slug).then((r) => r.ok && setLoginsHealth(r.items ?? []));
    getDhpUpgradeCheck(instance.id).then((r) => r.ok && setUpgrade(r));
  }, [instance.id, instance.slug, instance.config]);

  const save = async (changes: Record<string, unknown>) => {
    setSaving(true);
    setMsg(null);
    try {
      const r = await updateDigitalHumanInstance(instance.id, changes);
      if (r.ok) {
        setMsg({ ok: true, text: t("dh_edit.saved") });
      } else {
        setMsg({ ok: false, text: r.error || t("dh_edit.save_fail") });
      }
    } finally {
      setSaving(false);
    }
  };

  // Install a missing plugin dependency from the first enabled plugin source (defaults to
  // the Claude official marketplace). After install, refresh the plugins-health block so
  // the missing marker flips to installed.
  const installPluginDep = async (name: string) => {
    setInstallingDep(name);
    setMsg(null);
    try {
      const sources = await getPluginSources();
      const src = (sources ?? []).find((s) => s.enabled);
      if (!src) {
        setMsg({ ok: false, text: t("dh_edit.plugin_missing") });
        return;
      }
      const r = await installPlugin(src.id, name);
      if (r.ok) {
        setMsg({ ok: true, text: t("dh_edit.plugin_installed") });
        const h = await getDhpPluginsHealth(instance.slug);
        if (h.ok) setPluginsHealth(h.items ?? []);
      } else {
        setMsg({ ok: false, text: r.error || t("dh_edit.plugin_missing") });
      }
    } finally {
      setInstallingDep("");
    }
  };

  if (loading || !detail) {
    return <div className="px-6 py-8 text-[13px] text-faint text-center">{t("dh_edit.loading")}</div>;
  }

  const sp = detail.spec;

  return (
    <section className="px-6 py-4 pb-12">
      {/* header */}
      <button
        className="flex items-center gap-1.5 text-[13px] text-muted hover:text-ink mb-3"
        onClick={onBack}
      >
        <Icon name="arrowLeft" size={16} />
        {t("dh_edit.back")}
      </button>
      <h2 className="text-[17px] font-semibold text-ink mb-1">{sp.name}</h2>
      <p className="text-[13px] text-muted mb-4">{sp.description}</p>

      {msg && (
        <div className={"text-[12px] mb-3 " + (msg.ok ? "text-accent" : "text-warnInk")}>{msg.text}</div>
      )}

      {/* 1. 定时 */}
      <div className={GRP_H}>{t("dh_edit.blk_schedule")}</div>
      <div className={GRP + " px-4 py-3"}>
        <label className="flex items-center gap-2 text-[13px] text-ink mb-3 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => {
              setEnabled(e.target.checked);
              save({ enabled: e.target.checked });
            }}
          />
          {t("dh_edit.schedule_enable")}
        </label>
        <SchedulePicker
          cron={cron}
          onChange={(c) => {
            setCron(c);
            save({ cron: c });
          }}
        />
      </div>

      {/* 2. 模型（只读 spec.recommended_model，实际模型选择走 automation） */}
      <div className={GRP_H}>{t("dh_edit.blk_model")}</div>
      <div className={GRP + " px-4 py-3"}>
        <div className="text-[13px] text-ink">
          {sp.recommended_model ? `${t("dh_edit.model_recommended")}: ${sp.recommended_model}` : t("dh_edit.model_default")}
        </div>
        <div className="text-[11.5px] text-faint mt-1">{t("dh_edit.model_hint")}</div>
      </div>

      {/* 3. 能力（只读声明） */}
      <div className={GRP_H}>{t("dh_edit.blk_capabilities")}</div>
      <div className={GRP + " px-4 py-3"}>
        {detail.requires_consent.permissions.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {detail.requires_consent.permissions.map((p) => (
              <span key={p} className="text-[10.5px] font-semibold px-1.5 py-0.5 rounded bg-warnSoft text-warnInk">{p}</span>
            ))}
          </div>
        ) : (
          <div className="text-[13px] text-faint">{t("dh_edit.no_capabilities")}</div>
        )}
        <div className="text-[11.5px] text-faint mt-1">{t("dh_edit.capabilities_hint")}</div>
      </div>

      {/* 4. MCP 工具 */}
      <div className={GRP_H}>{t("dh_edit.blk_mcp")}</div>
      <div className={GRP + " px-4 py-3"}>
        {mcpHealth.length === 0 ? (
          <div className="text-[13px] text-faint">{t("dh_edit.no_mcp")}</div>
        ) : (
          mcpHealth.map((m) => (
            <div key={m.name} className="flex items-center gap-2 py-1">
              <span className={"w-2 h-2 rounded-full " + (m.configured ? "bg-ok" : "bg-warn")} />
              <span className="text-[13px] text-ink flex-1">{m.name}</span>
              <span className="text-[11.5px] text-faint">
                {m.configured ? `${m.status} · ${m.tool_count} tools` : t("dh_edit.mcp_missing")}
              </span>
            </div>
          ))
        )}
      </div>

      {/* 5. 技能 */}
      <div className={GRP_H}>{t("dh_edit.blk_skills")}</div>
      <div className={GRP + " px-4 py-3"}>
        {skillsHealth.length === 0 ? (
          <div className="text-[13px] text-faint">{t("dh_edit.no_skills")}</div>
        ) : (
          skillsHealth.map((s) => (
            <div key={s.id} className="flex items-center gap-2 py-1">
              <span className={"w-2 h-2 rounded-full " + (s.installed ? "bg-ok" : "bg-warn")} />
              <span className="text-[13px] text-ink flex-1">{s.id}</span>
              <span className="text-[11.5px] text-faint">{s.installed ? t("dh_edit.skill_installed") : t("dh_edit.skill_missing")}</span>
            </div>
          ))
        )}
      </div>

      {/* 5b. 插件（缺失项可一键从默认市场源补装） */}
      <div className={GRP_H}>{t("dh_edit.blk_plugins")}</div>
      <div className={GRP + " px-4 py-3"}>
        {pluginsHealth.length === 0 ? (
          <div className="text-[13px] text-faint">{t("dh_edit.no_plugins")}</div>
        ) : (
          pluginsHealth.map((p) => (
            <div key={p.id} className="flex items-center gap-2 py-1">
              <span className={"w-2 h-2 rounded-full " + (p.installed ? "bg-ok" : "bg-warn")} />
              <span className="text-[13px] text-ink flex-1">{p.id}</span>
              {p.installed ? (
                <span className="text-[11.5px] text-faint">{t("dh_edit.plugin_installed")}</span>
              ) : (
                <button
                  className="text-[11.5px] px-2 py-0.5 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent disabled:opacity-50"
                  disabled={installingDep === p.id}
                  onClick={() => installPluginDep(p.id)}
                >
                  {installingDep === p.id ? "…" : t("dh_edit.plugin_install")}
                </button>
              )}
            </div>
          ))
        )}
      </div>

      {/* 5c. 命令（只读，随插件打包安装，无独立市场源） */}
      <div className={GRP_H}>{t("dh_edit.blk_commands")}</div>
      <div className={GRP + " px-4 py-3"}>
        {commandsHealth.length === 0 ? (
          <div className="text-[13px] text-faint">{t("dh_edit.no_commands")}</div>
        ) : (
          commandsHealth.map((c) => (
            <div key={c.id} className="flex items-center gap-2 py-1">
              <span className={"w-2 h-2 rounded-full " + (c.installed ? "bg-ok" : "bg-warn")} />
              <span className="text-[13px] text-ink flex-1">{c.id}</span>
              <span className="text-[11.5px] text-faint">{c.installed ? t("dh_edit.command_installed") : t("dh_edit.command_missing")}</span>
            </div>
          ))
        )}
      </div>

      {/* 5d. 子代理（只读，市场源留 E5） */}
      <div className={GRP_H}>{t("dh_edit.blk_subagents")}</div>
      <div className={GRP + " px-4 py-3"}>
        {subagentsHealth.length === 0 ? (
          <div className="text-[13px] text-faint">{t("dh_edit.no_subagents")}</div>
        ) : (
          subagentsHealth.map((sa) => (
            <div key={sa.id} className="flex items-center gap-2 py-1">
              <span className={"w-2 h-2 rounded-full " + (sa.installed ? "bg-ok" : "bg-warn")} />
              <span className="text-[13px] text-ink flex-1">{sa.id}</span>
              <span className="text-[11.5px] text-faint">{sa.installed ? t("dh_edit.subagent_available") : t("dh_edit.subagent_missing")}</span>
            </div>
          ))
        )}
      </div>

      {/* 6. 登录（健康块：每项显示已登录/未登录 + 一键登录） */}
      <div className={GRP_H}>{t("dh_edit.blk_logins")}</div>
      <div className={GRP + " px-4 py-3"}>
        {loginsHealth.length === 0 ? (
          <div className="text-[13px] text-faint">{t("dh_edit.no_logins")}</div>
        ) : (
          loginsHealth.map((l, i) => {
            const chip = loginExpiryChip(l.expiry, t);
            return (
            <div key={i} className="flex items-center gap-2 py-1">
              <span className={"w-2 h-2 rounded-full " + (l.logged_in ? "bg-ok" : "bg-warn")} />
              <span className="text-[13px] text-ink flex-1 truncate">{l.label || l.url}</span>
              {l.logged_in ? (
                <span className="text-[11.5px] text-faint">
                  {chip ? chip.text : t("dh_edit.login_ready")}
                </span>
              ) : (
                <button
                  className="text-[11.5px] px-2 py-0.5 rounded-md border border-lineStrong bg-panel hover:border-accent hover:text-accent"
                  onClick={() => setLoginTarget({ url: l.url, label: l.label })}
                >
                  {t("dh_edit.login_go_manage")}
                </button>
              )}
            </div>
            );
          })
        )}
        <div className="text-[11.5px] text-faint mt-1">{t("dh_edit.logins_hint")}</div>
      </div>

      {/* Login capture modal — launched by the "登录" button above. Prefilled with the
          site's url/label so the user goes straight into the capture flow. onSaved re-fetches
          logins-health so the chip flips to logged-in. */}
      {loginTarget && (
        <BrowserLoginModal
          existing={null}
          presetUrl={loginTarget.url}
          presetLabel={loginTarget.label}
          onClose={() => setLoginTarget(null)}
          onSaved={() => {
            setLoginTarget(null);
            getDhpLoginsHealth(instance.slug).then((r) => r.ok && setLoginsHealth(r.items ?? []));
          }}
        />
      )}

      {/* 7. 通知 */}
      <DhNotifyBlock
        channels={notifyChannels}
        level={notifyLevel}
        onChannelsChange={(c) => { setNotifyChannels(c); save({ notify_channels: c }); }}
        onLevelChange={(l) => { setNotifyLevel(l); save({ notify_level: l }); }}
        saving={saving}
      />

      {/* 8. 配置 */}
      <DhConfigBlock
        schema={schema}
        config={userConfig}
        onChange={(cfg) => { setUserConfig(cfg); save({ user_config: cfg }); }}
        editable
        onSchemaChange={(s) => {
          setSchema(s);
          save({ config_schema: s });
        }}
      />

      {/* 9. 开发者（SystemPromptEditor 核心改进） */}
      <div className={GRP_H}>{t("dh_edit.blk_developer")}</div>
      <div className={GRP + " px-4 py-3"}>
        <div className="text-[12px] text-muted mb-1">{sp.name} · v{sp.version}</div>
        {sp.author && <div className="text-[11.5px] text-faint mb-2">author: {sp.author}</div>}
        <SystemPromptEditor
          value={systemPrompt}
          onChange={(v) => {
            setSystemPrompt(v);
            save({ system_prompt: v });
          }}
        />
      </div>

      {/* 10. 升级 */}
      <div className={GRP_H}>{t("dh_edit.blk_upgrade")}</div>
      <div className={GRP + " px-4 py-3"}>
        {upgrade ? (
          <div className="text-[13px] text-ink">
            {t("dh_edit.installed_v")}: {upgrade.installed_version || "—"} · {t("dh_edit.latest_v")}: {upgrade.latest_version || "—"}
            {!upgrade.up_to_date && (
              <span className="ml-2 text-[11px] font-semibold px-1.5 py-0.5 rounded bg-warnSoft text-warnInk">{t("dh_edit.upgrade_available")}</span>
            )}
          </div>
        ) : (
          <div className="text-[13px] text-faint">{t("dh_edit.upgrade_checking")}</div>
        )}
      </div>

      {/* 11. spec 信息 */}
      <div className={GRP_H}>{t("dh_edit.blk_spec")}</div>
      <div className={GRP + " px-4 py-3"}>
        <div className="text-[12px] text-muted">slug: {instance.slug}</div>
        <div className="text-[12px] text-muted">type: {sp.type || "automation"}</div>
        {sp.notify_channels.length > 0 && (
          <div className="text-[12px] text-muted">{t("dh_edit.spec_notify")}: {sp.notify_channels.join(", ")}</div>
        )}
      </div>

      {/* 危险区 */}
      <div className={GRP_H}>{t("dh_edit.blk_danger")}</div>
      <div className={GRP + " px-4 py-3"}>
        <button
          className="text-[12.5px] font-medium px-3 py-1.5 rounded-full border border-warnInk/30 text-warnInk hover:bg-warnSoft"
          onClick={() => {
            if (confirm(t("dh_edit.uninstall_confirm"))) {
              window.dispatchEvent(new CustomEvent("dh-uninstall", { detail: instance.id }));
            }
          }}
        >
          {t("dh_edit.uninstall")}
        </button>
      </div>
    </section>
  );
}
