// 数字人商店 + 安装 + 实例管理（批次 C）。
// 三栏心智：商店列表（分类筛选+搜索）→ 详情/安装表单（按 config_schema 动态渲染）→ 已装实例。
// 后端走 /v1/digital-humans/*（见 api.ts）。密钥字段（config_schema[].secret）走 password 框，
// 回显时不回填（与 notify 面板一致）。
import { useEffect, useMemo, useState } from "react";
import {
  getDigitalHumans,
  getDigitalHuman,
  installDigitalHuman,
  preflightDigitalHuman,
  getDigitalHumanInstances,
  uninstallDigitalHuman,
  type DigitalHumanEntry,
  type DigitalHumanDetail,
  type DigitalHumanInstance,
  type ConfigField,
  type DhpPreflight,
} from "../api";
import { useT } from "../i18n/I18nProvider";
import { GRP, GRP_H, ROW, PILL_ACCENT, PILL_LINE, TAG_ACCENT, TAG_QUIET, TAG_WARN, CHIP_OFF } from "./connectors/ui";
import { DhpSourcesSection } from "./DhpSourcesSection";
import { DhEditPanel } from "./dh-edit/DhEditPanel";
import { ConfigFieldInput } from "./dh-edit/ConfigFieldInput";
import { evalCondition } from "./dh-edit/cond";

const SEARCH_INPUT =
  "w-full px-3 py-2 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent";
const SCROLL = "overflow-y-auto";
const ITEM = "px-4 py-2.5 cursor-pointer hover:bg-paper transition-colors";
const ITEM_ACTIVE = "bg-accentSoft";

// 默认值优先用 config_schema 的 default；缺省按类型给一个合理空值。
function defaultFor(f: ConfigField): unknown {
  if (f.default !== undefined && f.default !== null) return f.default;
  switch (f.type) {
    case "number":
      return "";
    case "boolean":
      return false;
    case "stringList":
    case "urlList":
      return [];
    case "keyvalue":
      return [];
    case "json":
      return "";
    case "select":
      return f.multiple ? [] : "";
    default:
      return "";
  }
}

function seedConfig(fields: ConfigField[]): Record<string, unknown> {
  const cfg: Record<string, unknown> = {};
  for (const f of fields) cfg[f.key] = defaultFor(f);
  return cfg;
}

// 依赖清单的一行：label + 逗号分隔的 id 列表（preflight manifest 确认步骤用）。
function ManifestRow({ label, items }: { label: string; items: string[] }) {
  return (
    <div className="flex gap-2 text-[11px]">
      <span className="text-muted shrink-0">{label}:</span>
      <span className="text-ink">{items.join(", ")}</span>
    </div>
  );
}

export function DigitalHumansSection() {
  const { t } = useT();
  const [humans, setHumans] = useState<DigitalHumanEntry[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [activeCat, setActiveCat] = useState<string>("");
  const [query, setQuery] = useState("");
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [detail, setDetail] = useState<DigitalHumanDetail | null>(null);
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [installing, setInstalling] = useState(false);
  const [installMsg, setInstallMsg] = useState<{ ok: boolean; msg: string } | null>(null);
  const [preflight, setPreflight] = useState<DhpPreflight | null>(null);
  const [mcpAck, setMcpAck] = useState(false);
  const [instances, setInstances] = useState<DigitalHumanInstance[]>([]);
  const [editing, setEditing] = useState<DigitalHumanInstance | null>(null);

  const reload = () => {
    getDigitalHumans().then((r) => {
      setHumans(r.humans);
      setCategories(r.categories);
    }).catch(() => {});
    getDigitalHumanInstances().then((r) => setInstances(r.instances)).catch(() => {});
  };
  useEffect(reload, []);

  // On mount, honor a pending "open full edit" request stashed by ScheduledView.
  // The cross-surface deep-link (Automations ▸ 完整编辑) dispatches a window event,
  // but this section mounts only AFTER the Settings surface opens — so the event
  // fires before the listener exists. ScheduledView also stashes the id in
  // sessionStorage; we read it here and clear it so it only opens once.
  useEffect(() => {
    const pendingId = sessionStorage.getItem("dh:edit-instance");
    if (!pendingId) return;
    sessionStorage.removeItem("dh:edit-instance");
    getDigitalHumanInstances().then((r) => {
      const inst = r.instances.find((i) => i.id === pendingId);
      if (inst) setEditing(inst);
    });
  }, []);

  // listen for uninstall events from DhEditPanel's danger zone
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent;
      if (ce.detail) {
        uninstallDigitalHuman(ce.detail as string).then(() => {
          setEditing(null);
          reload();
        });
      }
    };
    window.addEventListener("dh-uninstall", handler);
    return () => window.removeEventListener("dh-uninstall", handler);
  }, []);

  // Deep-link: open DhEditPanel for an instance id (dispatched by ScheduledView's
  // "打开完整编辑" affordance on digital-human tasks).
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent;
      const instId = ce.detail as string;
      if (!instId) return;
      // Make sure instances are loaded, then open the editor for the requested id.
      getDigitalHumanInstances().then((r) => {
        const inst = r.instances.find((i) => i.id === instId);
        if (inst) setEditing(inst);
      });
    };
    window.addEventListener("dh:edit-instance", handler);
    return () => window.removeEventListener("dh:edit-instance", handler);
  }, []);

  const filtered = useMemo(() => {
    let list = humans;
    if (activeCat) list = list.filter((h) => h.category === activeCat);
    if (query.trim()) {
      const q = query.trim().toLowerCase();
      list = list.filter(
        (h) =>
          h.name.toLowerCase().includes(q) ||
          h.slug.toLowerCase().includes(q) ||
          h.description.toLowerCase().includes(q) ||
          h.tags.some((tag) => tag.toLowerCase().includes(q)),
      );
    }
    return list;
  }, [humans, activeCat, query]);

  const installedSlugs = useMemo(() => new Set(instances.map((i) => i.slug)), [instances]);

  const openDetail = async (slug: string) => {
    setSelectedSlug(slug);
    setInstallMsg(null);
    setDetail(null);
    try {
      const d = await getDigitalHuman(slug);
      if (d.ok) {
        setDetail(d);
        setConfig(seedConfig(d.spec.config_schema));
      } else {
        setInstallMsg({ ok: false, msg: d.error || t("digital.load_fail") });
      }
    } catch {
      setInstallMsg({ ok: false, msg: t("digital.load_fail") });
    }
  };

  const setField = (key: string, value: unknown) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  };

  const install = async () => {
    if (!detail) return;
    setInstalling(true);
    setInstallMsg(null);
    try {
      // Preflight: compute the dependency manifest + approval digest. The server
      // requires the digest back at install time so a spec that changed between
      // the user viewing the manifest and clicking install is caught.
      const pre = await preflightDigitalHuman(detail.entry.slug, config);
      if (!pre.ok || !pre.manifest || !pre.approval_digest) {
        setInstallMsg({ ok: false, msg: pre.error || t("digital.install_fail") });
        return;
      }
      setPreflight(pre);
      setMcpAck(false);
    } finally {
      setInstalling(false);
    }
  };

  const confirmInstall = async () => {
    if (!detail || !preflight?.approval_digest) return;
    setInstalling(true);
    try {
      const res = await installDigitalHuman(detail.entry.slug, config, {
        approval_digest: preflight.approval_digest,
        mcp_confirmed: mcpAck || !preflight.mcp_confirmation_required?.length,
      });
      if (res.ok) {
        setInstallMsg({ ok: true, msg: t("digital.install_ok") });
        setPreflight(null);
        reload();
      } else {
        // A digest mismatch means the spec changed since preflight — re-run preflight
        // so the user re-reviews the new manifest. Surface the error inline too.
        setInstallMsg({ ok: false, msg: res.error || t("digital.install_fail") });
        if (res.error && /changed|re-approve|manifest/i.test(res.error)) {
          setPreflight(null);
        }
      }
    } finally {
      setInstalling(false);
    }
  };

  const cancelPreflight = () => {
    setPreflight(null);
    setMcpAck(false);
  };

  const uninstall = async (instanceId: string) => {
    await uninstallDigitalHuman(instanceId);
    reload();
  };

  // 编辑态：展开 DhEditPanel，替换整个商店视图。
  if (editing) {
    return <DhEditPanel instance={editing} onBack={() => { setEditing(null); reload(); }} />;
  }

  return (
    <section className="px-6 py-4">
      {/* 源管理（最顶：列表空时用户第一反应是查源） */}
      <DhpSourcesSection />

      {/* 商店列表 */}
      <div className={GRP_H}>{t("digital.store_title")}</div>
      <div className="flex gap-2 mb-3">
        <input
          className={SEARCH_INPUT}
          placeholder={t("digital.search_placeholder")}
          value={query}
          spellCheck={false}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {/* 分类筛选 */}
      <div className="flex flex-wrap gap-1.5 mb-3">
        <button
          className={
            "text-[11.5px] px-2.5 py-1 rounded-full " +
            (!activeCat ? "bg-accent text-white" : "bg-paper border border-line text-muted")
          }
          onClick={() => setActiveCat("")}
        >
          {t("digital.cat_all")}
        </button>
        {categories.map((c) => (
          <button
            key={c}
            className={
              "text-[11.5px] px-2.5 py-1 rounded-full " +
              (activeCat === c ? "bg-accent text-white" : "bg-paper border border-line text-muted")
            }
            onClick={() => setActiveCat(c)}
          >
            {t("digital.cat_" + c) || c}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* 左：列表 */}
        <div className={GRP + " " + SCROLL + " max-h-[560px]"}>
          {filtered.length === 0 && (
            <div className="px-4 py-6 text-[13px] text-faint text-center">{t("digital.empty")}</div>
          )}
          {filtered.map((h) => {
            const active = selectedSlug === h.slug;
            const installed = installedSlugs.has(h.slug);
            return (
              <div
                key={h.slug}
                className={ROW + " " + ITEM + (active ? " " + ITEM_ACTIVE : "")}
                onClick={() => openDetail(h.slug)}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-medium text-ink truncate">{h.name}</span>
                    {installed && <span className={TAG_ACCENT}>{t("digital.installed_tag")}</span>}
                  </div>
                  <div className="text-[11.5px] text-faint truncate">{h.description}</div>
                  <div className="flex items-center gap-1.5 mt-0.5">
                    <span className={TAG_QUIET}>{h.category}</span>
                    {h.tags.slice(0, 2).map((tag) => (
                      <span key={tag} className="text-[10.5px] text-faint">
                        #{tag}
                      </span>
                    ))}
                  </div>
                </div>
                <span className="text-faint text-[11px] shrink-0">v{h.version}</span>
              </div>
            );
          })}
        </div>

        {/* 右：详情/安装 */}
        <div>
          {!detail ? (
            <div className="rounded-xl2 border border-line bg-panel px-4 py-8 text-center text-[13px] text-faint">
              {t("digital.select_hint")}
            </div>
          ) : (
            <div className="rounded-xl2 border border-line bg-panel px-4 py-4">
              <div className="flex items-center gap-2 mb-1">
                <h3 className="text-[15px] font-semibold text-ink">{detail.spec.name}</h3>
                <span className={TAG_QUIET}>v{detail.entry.version}</span>
              </div>
              <p className="text-[12.5px] text-muted mb-3">{detail.spec.description}</p>
              <div className="flex flex-wrap gap-1.5 mb-3">
                {detail.requires_consent.mcps.map((m) => (
                  <span key={m.id} className={TAG_WARN} title={m.reason}>
                    MCP: {m.id}
                  </span>
                ))}
                {detail.requires_consent.permissions.map((p) => (
                  <span key={p} className={TAG_WARN}>
                    {p}
                  </span>
                ))}
                {!detail.requires_consent.has_schedule && (
                  <span className={CHIP_OFF}>{t("digital.manual_run")}</span>
                )}
                {detail.spec.notify_channels.length > 0 && (
                  <span className={TAG_QUIET}>
                    {t("digital.notify_label")}: {detail.spec.notify_channels.join(", ")}
                  </span>
                )}
              </div>

              {detail.spec.config_schema.length > 0 && (
                <>
                  <div className="text-[12px] font-semibold text-muted mb-2">{t("digital.config_title")}</div>
                  {detail.spec.config_schema
                    .filter((f) => evalCondition(f.visible_if, config))
                    .map((f) => (
                    <ConfigFieldInput key={f.key} f={f} value={config[f.key]} onChange={(v) => setField(f.key, v)} t={t} />
                  ))}
                </>
              )}

              {installMsg && (
                <div className={"text-[12px] mt-2 " + (installMsg.ok ? "text-accent" : "text-warnInk")}>
                  {installMsg.msg}
                </div>
              )}

              {preflight && preflight.manifest ? (
                <div className="mt-3 rounded-lg border border-line bg-paperSoft p-3 space-y-2">
                  <div className="text-[12px] font-semibold text-ink">{t("digital.manifest_title")}</div>
                  <div className="text-[11px] text-muted">
                    {t("digital.manifest_version")}: {preflight.manifest.version}
                    {preflight.manifest.source ? ` · ${preflight.manifest.source}` : ""}
                  </div>
                  {preflight.manifest.requires_plugins.length > 0 && (
                    <ManifestRow label={t("digital.manifest_plugins")} items={preflight.manifest.requires_plugins.map((p) => `${p.id}${p.bundled ? " (bundled)" : ""}`)} />
                  )}
                  {preflight.manifest.requires_mcps.length > 0 && (
                    <ManifestRow label={t("digital.manifest_mcps")} items={preflight.manifest.requires_mcps.map((m) => m.id)} />
                  )}
                  {preflight.manifest.requires_skills.length > 0 && (
                    <ManifestRow label={t("digital.manifest_skills")} items={preflight.manifest.requires_skills.map((s) => s.id)} />
                  )}
                  {preflight.manifest.requires_commands.length > 0 && (
                    <ManifestRow label={t("digital.manifest_commands")} items={preflight.manifest.requires_commands.map((c) => c.id)} />
                  )}
                  {preflight.manifest.requires_subagents.length > 0 && (
                    <ManifestRow label={t("digital.manifest_subagents")} items={preflight.manifest.requires_subagents.map((s) => s.id)} />
                  )}
                  {preflight.manifest.permissions.length > 0 && (
                    <ManifestRow label={t("digital.manifest_permissions")} items={preflight.manifest.permissions} />
                  )}
                  {preflight.manifest.config_secret_keys.length > 0 && (
                    <ManifestRow label={t("digital.manifest_secrets")} items={preflight.manifest.config_secret_keys} />
                  )}
                  {preflight.mcp_confirmation_required && preflight.mcp_confirmation_required.length > 0 && (
                    <label className="flex items-start gap-2 text-[11px] text-ink mt-1">
                      <input type="checkbox" checked={mcpAck} onChange={(e) => setMcpAck(e.target.checked)} className="mt-0.5" />
                      <span>
                        {t("digital.manifest_mcp_ack")}
                        <span className="text-muted"> ({preflight.mcp_confirmation_required.join(", ")})</span>
                      </span>
                    </label>
                  )}
                  <div className="flex gap-2 pt-1">
                    <button
                      className={PILL_ACCENT}
                      onClick={confirmInstall}
                      disabled={installing || (!!preflight.mcp_confirmation_required?.length && !mcpAck)}
                    >
                      {installing ? t("digital.installing") : t("digital.manifest_confirm")}
                    </button>
                    <button className={PILL_LINE} onClick={cancelPreflight} disabled={installing}>
                      {t("digital.manifest_cancel")}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex gap-2 mt-3">
                  <button className={PILL_ACCENT} onClick={install} disabled={installing}>
                    {installing ? t("digital.installing") : t("digital.install_btn")}
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 已装实例 */}
      <div className={GRP_H}>{t("digital.instances_title")}</div>
      <div className={GRP}>
        {instances.length === 0 ? (
          <div className="px-4 py-5 text-[13px] text-faint text-center">{t("digital.no_instances")}</div>
        ) : (
          instances.map((inst) => (
            <div key={inst.id} className={ROW}>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium text-ink">{inst.name}</span>
                  <span className={TAG_QUIET}>{inst.slug}</span>
                  {inst.task?.enabled ? (
                    <span className={TAG_ACCENT}>{t("digital.enabled")}</span>
                  ) : (
                    <span className={CHIP_OFF}>{t("digital.disabled")}</span>
                  )}
                </div>
                <div className="text-[11.5px] text-faint">
                  {inst.task?.schedule || t("digital.manual_run")}
                  {inst.task?.last_status ? ` · ${t("digital.last_status")}: ${inst.task.last_status}` : ""}
                  {inst.task?.run_count ? ` · ${inst.task.run_count} ${t("digital.runs")}` : ""}
                </div>
              </div>
              <div className="flex gap-2 shrink-0">
                <button className={PILL_ACCENT} onClick={() => setEditing(inst)}>
                  {t("dh_edit.edit")}
                </button>
                <button className={PILL_LINE} onClick={() => uninstall(inst.id)}>
                  {t("digital.uninstall")}
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

