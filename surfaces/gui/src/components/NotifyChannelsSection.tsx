// 通知渠道配置面板 —— 钉钉/飞书/企微/通用 webhook/邮件。
// 每个渠道一张卡：动态字段 + 启用开关 + 保存 + 测试发送。
// 后端走 /v1/notify/* 端点（见 api.ts）。密钥字段存 SecretStore，回显时脱敏。
import { useEffect, useState } from "react";
import {
  getNotifyChannels,
  getNotifyChannelConfig,
  saveNotifyChannelConfig,
  testNotifyChannel,
  type NotifyChannelInfo,
} from "../api";
import { useT } from "../i18n/I18nProvider";
import { GRP, GRP_H, ROW, PILL_ACCENT, PILL_LINE, TAG_QUIET, TAG_ACCENT } from "./connectors/ui";

const INPUT =
  "px-3 py-1.5 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent w-full";
const LABEL = "text-[12px] font-medium text-muted mb-1 block";

// 每个渠道的字段定义。type: text/password 决定回显是否脱敏。
type Field = { key: string; labelKey: string; type: "text" | "password"; placeholder?: string };
const FIELDS: Record<string, Field[]> = {
  dingtalk: [
    { key: "url", labelKey: "notify.field_webhook_url", type: "text", placeholder: "https://oapi.dingtalk.com/robot/send?access_token=..." },
    { key: "secret", labelKey: "notify.field_sign_secret", type: "password", placeholder: "SEC..." },
  ],
  feishu: [
    { key: "url", labelKey: "notify.field_webhook_url", type: "text", placeholder: "https://open.feishu.cn/open-apis/bot/v2/hook/..." },
    { key: "secret", labelKey: "notify.field_sign_secret", type: "password" },
  ],
  wecom: [
    { key: "url", labelKey: "notify.field_webhook_url", type: "text", placeholder: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." },
    { key: "secret", labelKey: "notify.field_sign_secret", type: "password" },
  ],
  webhook: [
    { key: "url", labelKey: "notify.field_webhook_url", type: "text", placeholder: "https://..." },
  ],
  email: [
    { key: "smtp_host", labelKey: "notify.field_smtp_host", type: "text", placeholder: "smtp.example.com" },
    { key: "smtp_port", labelKey: "notify.field_smtp_port", type: "text", placeholder: "465" },
    { key: "username", labelKey: "notify.field_smtp_user", type: "text", placeholder: "you@example.com" },
    { key: "password", labelKey: "notify.field_smtp_pass", type: "password" },
    { key: "to_addr", labelKey: "notify.field_smtp_to", type: "text", placeholder: "recipient@example.com" },
  ],
};

type ConfigMap = Record<string, Record<string, string>>;
type TestState = Record<string, { ok: boolean; msg: string } | null>;

export function NotifyChannelsSection() {
  const { t } = useT();
  const [channels, setChannels] = useState<NotifyChannelInfo[]>([]);
  const [configs, setConfigs] = useState<ConfigMap>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [testState, setTestState] = useState<TestState>({});

  const reload = () => {
    getNotifyChannels().then((r) => setChannels(r.channels)).catch(() => {});
  };
  useEffect(reload, []);

  const open = async (ch: string) => {
    setExpanded(expanded === ch ? null : ch);
    if (expanded !== ch && !configs[ch]) {
      try {
        const r = await getNotifyChannelConfig(ch);
        // 脱敏的密钥字段（"••••••"）不回填到表单，避免覆盖；只填非敏感字段。
        const cfg = r.config || {};
        const filled: Record<string, string> = {};
        for (const f of FIELDS[ch] || []) {
          const v = (cfg as any)[f.key];
          if (v && v !== "••••••") filled[f.key] = String(v);
        }
        setConfigs((prev) => ({ ...prev, [ch]: filled }));
      } catch {
        /* ignore */
      }
    }
  };

  const setField = (ch: string, key: string, value: string) => {
    setConfigs((prev) => ({ ...prev, [ch]: { ...(prev[ch] || {}), [key]: value } }));
  };

  const save = async (ch: string) => {
    setSaving(ch);
    try {
      const cfg = configs[ch] || {};
      // 合并 enabled 状态。enabled 单独由 toggle 管理，不在这里丢。
      const enabled = channels.find((c) => c.channel === ch)?.enabled ?? false;
      const res = await saveNotifyChannelConfig(ch, { ...cfg, enabled });
      if (res.ok) {
        setTestState((prev) => ({ ...prev, [ch]: { ok: true, msg: t("notify.saved") } }));
        reload();
      } else {
        setTestState((prev) => ({ ...prev, [ch]: { ok: false, msg: res.error || t("notify.save_fail") } }));
      }
    } finally {
      setSaving(null);
    }
  };

  const test = async (ch: string) => {
    setTesting(ch);
    setTestState((prev) => ({ ...prev, [ch]: null }));
    try {
      const cfg = configs[ch] || {};
      const enabled = channels.find((c) => c.channel === ch)?.enabled ?? false;
      const res = await testNotifyChannel(ch, { ...cfg, enabled });
      const r = res.results?.[0];
      if (res.ok && r?.ok) {
        setTestState((prev) => ({ ...prev, [ch]: { ok: true, msg: t("notify.test_ok") } }));
      } else {
        setTestState((prev) => ({ ...prev, [ch]: { ok: false, msg: r?.error || t("notify.test_fail") } }));
      }
    } finally {
      setTesting(null);
    }
  };

  const toggleEnabled = async (ch: string, enabled: boolean) => {
    const cfg = configs[ch] || {};
    await saveNotifyChannelConfig(ch, { ...cfg, enabled });
    setChannels((prev) => prev.map((c) => (c.channel === ch ? { ...c, enabled } : c)));
  };

  return (
    <section className="px-6 py-4 max-w-2xl">
      <div className={GRP_H}>{t("notify.section_title")}</div>
      <div className={GRP}>
        {channels.map((c) => {
          const isOpen = expanded === c.channel;
          const fields = FIELDS[c.channel] || [];
          const ts = testState[c.channel];
          return (
            <div key={c.channel}>
              <div className={ROW + " cursor-pointer select-none"} onClick={() => open(c.channel)}>
                <span className="text-[13px] font-medium text-ink flex-1">{c.label}</span>
                {c.configured ? (
                  <span className={c.enabled ? TAG_ACCENT : TAG_QUIET}>
                    {c.enabled ? t("notify.enabled") : t("notify.disabled")}
                  </span>
                ) : (
                  <span className={TAG_QUIET}>{t("notify.not_configured")}</span>
                )}
                <span className="text-faint text-[12px]">{isOpen ? "▾" : "▸"}</span>
              </div>
              {isOpen && (
                <div className="px-4 py-3 bg-paper">
                  <label className="flex items-center gap-2 text-[13px] text-ink mb-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={c.enabled}
                      onChange={(e) => toggleEnabled(c.channel, e.target.checked)}
                    />
                    {t("notify.enable_label")}
                  </label>
                  <p className="text-[12px] text-faint mb-3">{c.description}</p>
                  {fields.map((f) => (
                    <div key={f.key} className="mb-2.5">
                      <label className={LABEL}>{t(f.labelKey)}</label>
                      <input
                        className={INPUT}
                        type={f.type === "password" ? "password" : "text"}
                        placeholder={f.placeholder}
                        value={(configs[c.channel] || {})[f.key] || ""}
                        spellCheck={false}
                        onChange={(e) => setField(c.channel, f.key, e.target.value)}
                      />
                    </div>
                  ))}
                  {ts && (
                    <div className={"text-[12px] mt-2 " + (ts.ok ? "text-accent" : "text-warnInk")}>
                      {ts.msg}
                    </div>
                  )}
                  <div className="flex gap-2 mt-3">
                    <button
                      className={PILL_ACCENT}
                      onClick={() => save(c.channel)}
                      disabled={saving === c.channel}
                    >
                      {saving === c.channel ? t("notify.saving") : t("notify.save")}
                    </button>
                    <button
                      className={PILL_LINE}
                      onClick={() => test(c.channel)}
                      disabled={testing === c.channel}
                    >
                      {testing === c.channel ? t("notify.testing") : t("notify.test")}
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
