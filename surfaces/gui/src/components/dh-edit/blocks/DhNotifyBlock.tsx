// DhNotifyBlock — 数字人通知渠道 + 级别区块（批次 D2）。
// 级别 pill(all/important/none) + 渠道勾选(GET /v1/notify/channels)。
// 保存：onChannelsChange/onLevelChange → PATCH instance.notify_channels / notify_level。
import { useEffect, useState } from "react";
import { getNotifyChannels, type NotifyChannelInfo } from "../../../api";
import { useT } from "../../../i18n/I18nProvider";
import { GRP, GRP_H } from "../../connectors/ui";

const PILL =
  "text-[12px] px-2.5 py-1 rounded-full border transition-colors shrink-0";

export function DhNotifyBlock({
  channels,
  level,
  onChannelsChange,
  onLevelChange,
  saving,
}: {
  channels: string[];
  level: string;
  onChannelsChange: (c: string[]) => void;
  onLevelChange: (l: string) => void;
  saving: boolean;
}) {
  const { t } = useT();
  const [available, setAvailable] = useState<NotifyChannelInfo[]>([]);

  useEffect(() => {
    getNotifyChannels()
      .then((r) => setAvailable(r.channels))
      .catch(() => {});
  }, []);

  const toggleChannel = (name: string) => {
    if (channels.includes(name)) {
      onChannelsChange(channels.filter((c) => c !== name));
    } else {
      onChannelsChange([...channels, name]);
    }
  };

  const levels = [
    { key: "all", label: t("dh_edit.notify_all") },
    { key: "important", label: t("dh_edit.notify_important") },
    { key: "none", label: t("dh_edit.notify_none") },
  ];

  return (
    <>
      <div className={GRP_H}>{t("dh_edit.blk_notify")}</div>
      <div className={GRP + " px-4 py-3"}>
        {/* level pills */}
        <div className="flex flex-wrap gap-1.5 mb-3">
          {levels.map((l) => (
            <button
              key={l.key}
              className={PILL + (level === l.key ? " bg-accent text-white border-accent" : " bg-paper border-line text-muted")}
              onClick={() => onLevelChange(l.key)}
              disabled={saving}
            >
              {l.label}
            </button>
          ))}
        </div>
        {/* channels */}
        <div className="text-[12px] text-muted mb-1.5">{t("dh_edit.notify_channels_hint")}</div>
        {available.length === 0 ? (
          <div className="text-[13px] text-faint">{t("dh_edit.notify_no_channels")}</div>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {available.map((c) => {
              const active = channels.includes(c.channel);
              return (
                <button
                  key={c.channel}
                  className={PILL + (active ? " bg-accent text-white border-accent" : " bg-paper border-line text-muted")}
                  onClick={() => toggleChannel(c.channel)}
                  disabled={saving}
                  title={c.description}
                >
                  {c.label}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </>
  );
}
