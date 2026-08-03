// 数字人源管理（批次 D2）：仿 halo RegistrySection。
// 列表（name + url + enabled toggle + 删除[内置隐藏]）+ 添加表单（Name + URL）。
// 挂在 Settings「数字人」tab 内，商店列表上方（列表空时用户第一反应是查源）。
import { useEffect, useState, useCallback } from "react";
import {
  getDhpSources,
  addDhpSource,
  updateDhpSource,
  removeDhpSource,
  resetDhpSources,
  type DhpSource,
} from "../api";
import { useT } from "../i18n/I18nProvider";
import { GRP, GRP_H, ROW } from "./connectors/ui";
import { Icon } from "./Icon";

const INPUT =
  "px-3 py-1.5 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent w-full";

export function DhpSourcesSection() {
  const { t } = useT();
  const [sources, setSources] = useState<DhpSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState("");
  const [newUrl, setNewUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setLoading(true);
      const r = await getDhpSources();
      if (r.ok) setSources(r.sources);
    } catch {
      /* swallow */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const handleAdd = async () => {
    const name = newName.trim();
    const url = newUrl.trim();
    if (!name || !url) {
      setErr(t("dhp_sources.err_name_url"));
      return;
    }
    try {
      new URL(url);
    } catch {
      setErr(t("dhp_sources.err_bad_url"));
      return;
    }
    setAdding(true);
    setErr(null);
    const r = await addDhpSource(name, url);
    setAdding(false);
    if (r.ok) {
      setNewName("");
      setNewUrl("");
      setShowAdd(false);
      await reload();
    } else {
      setErr(r.error || t("dhp_sources.add_fail"));
    }
  };

  const toggle = async (src: DhpSource, enabled: boolean) => {
    await updateDhpSource(src.id, { enabled });
    await reload();
  };

  const remove = async (id: string) => {
    await removeDhpSource(id);
    await reload();
  };

  const reset = async () => {
    await resetDhpSources();
    await reload();
  };

  return (
    <>
      <div className={GRP_H}>{t("dhp_sources.title")}</div>
      <div className={GRP}>
        {loading ? (
          <div className={ROW}>
            <Icon name="refresh" size={16} className="animate-spin text-faint" />
            <span className="text-[13px] text-faint">{t("common.loading")}</span>
          </div>
        ) : sources.length === 0 ? (
          <div className={ROW + " justify-center text-[13px] text-faint"}>
            {t("dhp_sources.empty")}
          </div>
        ) : (
          sources.map((src) => (
            <div key={src.id} className={ROW}>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium text-ink truncate">{src.name}</span>
                  {src.is_default && (
                    <span className="text-[10.5px] font-semibold px-1.5 py-0.5 rounded bg-paper border border-line text-muted shrink-0">
                      {t("dhp_sources.default")}
                    </span>
                  )}
                  <span className="text-[10.5px] font-semibold px-1.5 py-0.5 rounded bg-accentSoft text-accent shrink-0">
                    {src.source_type === "local" ? t("dhp_sources.type_local") : t("dhp_sources.type_http")}
                  </span>
                </div>
                <div className="text-[11.5px] text-faint truncate mt-0.5">{src.url}</div>
              </div>
              {/* toggle */}
              <label className="relative inline-flex items-center cursor-pointer shrink-0">
                <input
                  type="checkbox"
                  checked={src.enabled}
                  onChange={() => toggle(src, !src.enabled)}
                  className="sr-only peer"
                />
                <div className="w-10 h-5.5 bg-paper border border-line rounded-full peer peer-checked:bg-accent transition-colors">
                  <div
                    className={`w-4.5 h-4.5 bg-white rounded-full shadow-sm transform transition-transform mt-0.5 ${
                      src.enabled ? "translate-x-[22px]" : "translate-x-0.5"
                    }`}
                  />
                </div>
              </label>
              <button
                type="button"
                onClick={() => remove(src.id)}
                className="text-faint hover:text-danger transition-colors shrink-0"
                title={src.is_default ? t("dhp_sources.remove_official") : t("dhp_sources.remove")}
              >
                <Icon name="trash" size={16} />
              </button>
            </div>
          ))
        )}

        {showAdd && (
          <div className="px-4 py-3 space-y-2 border-t border-line">
            <input
              className={INPUT}
              placeholder={t("dhp_sources.name_ph")}
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              spellCheck={false}
            />
            <input
              className={INPUT}
              placeholder={t("dhp_sources.url_ph")}
              value={newUrl}
              onChange={(e) => setNewUrl(e.target.value)}
              spellCheck={false}
            />
            {err && <div className="text-[12px] text-warnInk">{err}</div>}
            <div className="flex gap-2">
              <button
                className="text-[12.5px] font-medium px-3 py-1.5 rounded-full bg-accent text-white disabled:opacity-50"
                onClick={handleAdd}
                disabled={adding}
              >
                {adding ? t("dhp_sources.adding") : t("dhp_sources.add")}
              </button>
              <button
                className="text-[12.5px] px-3 py-1.5 rounded-full text-muted hover:text-ink"
                onClick={() => {
                  setShowAdd(false);
                  setNewName("");
                  setNewUrl("");
                  setErr(null);
                }}
              >
                {t("common.cancel")}
              </button>
            </div>
          </div>
        )}
      </div>

      {!showAdd && !loading && (
        <div className="mt-2 flex items-center gap-3">
          <button
            className="flex items-center gap-1.5 text-[12.5px] text-accent hover:underline"
            onClick={() => setShowAdd(true)}
          >
            <Icon name="plus" size={16} />
            {t("dhp_sources.add")}
          </button>
          <button
            className="flex items-center gap-1.5 text-[12.5px] text-muted hover:text-ink"
            onClick={reset}
            title={t("dhp_sources.reset_help")}
          >
            <Icon name="refresh" size={14} />
            {t("dhp_sources.reset")}
          </button>
        </div>
      )}
    </>
  );
}
