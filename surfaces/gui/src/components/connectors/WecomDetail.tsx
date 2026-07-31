import { useEffect, useState } from "react";
import {
  disconnectConnector,
  getWecomStatus,
  type WecomStatus,
} from "../../api";
import { ConnectorBadge } from "../../connectors/ConnectorIcon";
import { AddConnectionModal } from "./AddConnectionModal";
import type { DetailProps } from "./ConnectorsSection";
import { AllowlistBlock, ConnectorTools, UnauthorizedBlock } from "../ManageTabs";
import { ToolsDisclosure } from "./ToolsDisclosure";
import {
  FOOT,
  GRP,
  GRP_H,
  PILL_LINE,
  ROW,
  TAG_QUIET,
} from "./ui";
import { useT, currentLang } from "../../i18n/I18nProvider";
import zh from "../../i18n/zh.json";
import en from "../../i18n/en.json";

// 企微自建应用详情页：与 SlackDetail 同级，但更轻——企微是 1v1 私聊，无频道订阅。
// 核心独有块：回调 URL + 加密模式状态（让用户知道往企微后台填什么）。
// 复用通用 AllowlistBlock / UnauthorizedBlock（捕获 userid + 白名单）+ ConnectorTools + 断开。

type Dict = Record<string, string>;
const DICTS: Record<string, Dict> = { zh: zh as Dict, en: en as Dict };
const EN: Dict = en as Dict;
function tt(key: string, params?: Record<string, string | number>): string {
  const d = DICTS[currentLang()] ?? DICTS.zh;
  let raw = d[key] ?? EN[key] ?? key;
  if (params) for (const [k, v] of Object.entries(params)) raw = raw.replace(`{${k}}`, String(v));
  return raw;
}

const LABEL = "text-[12.5px] text-muted w-28 shrink-0";
const VALUE = "text-[13px] text-ink min-w-0 flex-1 font-mono break-all";

export function WecomDetail({ c, cloud, onChanged }: DetailProps) {
  const { t } = useT();
  const [adding, setAdding] = useState(false);
  const [status, setStatus] = useState<WecomStatus | null>(null);

  useEffect(() => {
    getWecomStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

  if (!c.connected) {
    // 未连接：走通用 AddConnectionModal（descriptor 驱动的表单）。
    return (
      <div>
        <div className="flex items-center gap-3.5 mb-5">
          <ConnectorBadge connector={c} size={44} title={c.title} />
          <div className="min-w-0 flex-1">
            <h2 className="text-[20px] font-semibold tracking-tight leading-tight">{c.title}</h2>
            <p className="text-[12.5px] text-muted">{c.blurb}</p>
          </div>
        </div>
        <button className={PILL_LINE} onClick={() => setAdding(true)}>
          {t("conn.connect")}
        </button>
        {adding && (
          <AddConnectionModal
            c={c}
            cloud={cloud}
            onClose={() => setAdding(false)}
            onChanged={onChanged}
          />
        )}
        <WecomHowItWorks />
      </div>
    );
  }

  const encrypted = status?.encrypted;
  const callbackPath = "/v1/connectors/wecom/callback";

  return (
    <div>
      <div className="flex items-center gap-3.5 mb-5">
        <ConnectorBadge connector={c} size={44} title={c.title} />
        <div className="min-w-0 flex-1">
          <h2 className="text-[20px] font-semibold tracking-tight leading-tight">{c.title}</h2>
          <div className="text-[12.5px] text-muted flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-ok" />
            {c.account || tt("wecom.connected")}
            {encrypted !== undefined && (
              <span className={encrypted ? TAG_QUIET : "text-[10.5px] font-semibold px-1.5 py-0.5 rounded bg-warnSoft text-warnInk shrink-0"}>
                {encrypted ? tt("wecom.mode_encrypted") : tt("wecom.mode_plain")}
              </span>
            )}
          </div>
        </div>
        <button
          className="text-[12.5px] text-danger/80 hover:text-danger shrink-0"
          onClick={async () => {
            await disconnectConnector(c.name);
            onChanged();
          }}
        >
          {tt("conn.disconnect")}
        </button>
      </div>

      {/* 回调配置 —— 企微后台「接收消息」要填的 URL */}
      <div className={GRP_H}>{tt("wecom.callback_group")}</div>
      <div className={GRP}>
        <div className={ROW + " flex-col items-start gap-1 py-3"}>
          <span className="text-[12px] text-muted">{tt("wecom.callback_url_label")}</span>
          <span className={VALUE}>https://你的公网域名{callbackPath}</span>
          <span className="text-[12px] text-faint">{tt("wecom.callback_url_hint")}</span>
        </div>
        <div className={ROW}>
          <span className={LABEL}>{tt("wecom.encrypt_mode")}</span>
          <span className={VALUE}>
            {encrypted === undefined
              ? "—"
              : encrypted
                ? tt("wecom.encrypted_on")
                : tt("wecom.encrypted_off")}
          </span>
          {status?.agent_id && (
            <span className={TAG_QUIET}>AgentId {status.agent_id}</span>
          )}
        </div>
      </div>
      <div className={FOOT}>{tt("wecom.callback_foot")}</div>

      {/* 工具 */}
      <div className={GRP + " mt-4"}>
        <ConnectorTools c={c} onChanged={onChanged} />
      </div>

      {/* 白名单 + 捕获（two-way 复用通用块）*/}
      <div className={GRP + " mt-4"}>
        <AllowlistBlock c={c} onChanged={onChanged} />
        <UnauthorizedBlock c={c} onChanged={onChanged} />
      </div>

      <ToolsDisclosure c={c} onChanged={onChanged} />
      {adding && (
        <AddConnectionModal
          c={c}
          cloud={cloud}
          onClose={() => setAdding(false)}
          onChanged={onChanged}
        />
      )}
    </div>
  );
}

function WecomHowItWorks() {
  return (
    <div className="mt-5">
      <div className={GRP_H}>{tt("wecom.how_title")}</div>
      <div className={GRP}>
        <div className={ROW + " items-start flex-col gap-1 py-3"}>
          <p className="text-[13px] text-ink leading-relaxed">{tt("wecom.how_step1")}</p>
          <p className="text-[13px] text-ink leading-relaxed">{tt("wecom.how_step2")}</p>
          <p className="text-[13px] text-ink leading-relaxed">{tt("wecom.how_step3")}</p>
          <p className="text-[13px] text-muted leading-relaxed">{tt("wecom.how_step4")}</p>
        </div>
      </div>
      <div className={FOOT}>{tt("wecom.how_foot")}</div>
    </div>
  );
}
