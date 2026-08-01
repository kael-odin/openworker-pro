import { useEffect, useMemo, useState } from "react";
import {
  disconnectWeChatIlinkAccount,
  getDmRoute,
  setDefaultWeChatIlinkAccount,
  type WeChatIlinkAccount,
} from "../../api";
import { ConnectorBadge } from "../../connectors/ConnectorIcon";
import { useT } from "../../i18n/I18nProvider";
import { AllowlistBlock, UnauthorizedBlock } from "../ManageTabs";
import type { DetailProps } from "./ConnectorsSection";
import { WeChatIlinkQrFlow } from "./WeChatIlinkQrFlow";
import { FOOT, GRP, GRP_H, PILL_ACCENT, TAG_ACCENT, TAG_QUIET, TAG_WARN } from "./ui";

export function WeChatIlinkDetail({ c, onChanged }: DetailProps) {
  const { t } = useT();
  const accounts = (c.accounts ?? []) as WeChatIlinkAccount[];
  const [qrMode, setQrMode] = useState<{ accountId?: string } | null>(null);
  const aggregateState = c.status?.state || aggregate(accounts);
  // Whether a DM route is configured. Without it, inbound WeChat DMs land in the Inbox's
  // "Unrouted" dead-letter (the user has no session designated to receive them). Shown as a
  // prompt here so the user learns to set it from the Inbox instead of wondering why messages
  // never reach a conversation.
  const [dmRouted, setDmRouted] = useState<boolean | null>(null);
  const refreshDmRoute = () => {
    getDmRoute().then((s) => setDmRouted(s !== null)).catch(() => setDmRouted(null));
  };
  useEffect(() => {
    refreshDmRoute();
  }, [c.name]);

  const openInbox = () => {
    window.dispatchEvent(new CustomEvent("coworker:open-inbox"));
  };

  return (
    <div data-testid="ilink-detail">
      <div className="flex items-center gap-3.5 mb-5">
        <ConnectorBadge connector={c} size={44} title={c.title} />
        <div className="min-w-0 flex-1">
          <h2 className="text-[20px] font-semibold tracking-tight leading-tight">{c.title}</h2>
          <div className="text-[12.5px] text-muted flex items-center gap-1.5">
            <span className={stateDot(aggregateState)} />
            {t(`ilink.state_${aggregateState}`)} · {t(accounts.length === 1 ? "conn.account_n_one" : "conn.account_n", { n: accounts.length })}
          </div>
        </div>
        <button className={PILL_ACCENT} data-testid="ilink-add-account" onClick={() => setQrMode({})}>
          {t("conn.add_account")}
        </button>
      </div>

      <p className="text-[13px] text-muted leading-relaxed mb-4">{t("ilink.detail_intro")}</p>

      {dmRouted === false && (
        <div
          className="flex items-start gap-3 rounded-xl border border-line bg-panel px-4 py-3 mb-4"
          data-testid="ilink-dm-route-hint"
        >
          <span className="text-[15px] leading-none mt-0.5">💡</span>
          <div className="min-w-0 flex-1 text-[13px] leading-relaxed">
            <div className="font-medium mb-0.5">{t("ilink.dm_route_title")}</div>
            <div className="text-muted">{t("ilink.dm_route_hint")}</div>
          </div>
          <button className={PILL_ACCENT + " shrink-0"} onClick={openInbox}>
            {t("ilink.dm_route_go")}
          </button>
        </div>
      )}

      <div className={GRP_H + " !mt-0"}>{t("conn.accounts")}</div>
      <div className={GRP} data-testid="ilink-accounts">
        {accounts.map((account) => (
          <AccountCard
            key={account.account_id}
            c={c}
            account={account}
            onChanged={onChanged}
            onReauth={() => setQrMode({ accountId: account.account_id })}
          />
        ))}
      </div>
      <div className={FOOT}>{t("ilink.accounts_foot")}</div>

      <div className={GRP_H}>{t("ilink.capabilities_title")}</div>
      <div className={GRP}>
        {["ilink.capability_dm", "ilink.capability_media", "ilink.capability_context", "ilink.capability_protocol"].map((key) => (
          <div className="px-4 py-2.5 text-[13px]" key={key}>{t(key)}</div>
        ))}
      </div>
      <div className={FOOT}>{t("ilink.disconnect_foot")}</div>

      {qrMode && (
        <QrDialog
          cTitle={c.title}
          accountId={qrMode.accountId}
          onClose={() => setQrMode(null)}
          onConfirmed={() => {
            setQrMode(null);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

function AccountCard({
  c,
  account,
  onChanged,
  onReauth,
}: {
  c: DetailProps["c"];
  account: WeChatIlinkAccount;
  onChanged: () => void;
  onReauth: () => void;
}) {
  const { t } = useT();
  const [busy, setBusy] = useState(false);
  const lastSeen = useMemo(
    () => account.last_event_at ? new Date(account.last_event_at * 1000).toLocaleString() : "",
    [account.last_event_at],
  );

  return (
    <section data-testid={`ilink-account-${account.account_id}`}>
      <div className="px-4 py-3 flex items-start gap-3">
        <span className={stateDot(account.state) + " mt-1.5"} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-[13px] truncate">{account.display_name}</span>
            {account.default && <span className={TAG_ACCENT}>{t("conn.default")}</span>}
            <span className={stateTag(account.state)}>{t(`ilink.state_${account.state}`)}</span>
          </div>
          <div className="text-[11.5px] text-faint truncate" title={account.account_id}>{account.account_id}</div>
          {account.state === "reconnecting" && (
            <div className="text-[11.5px] text-warnInk mt-1">{t("ilink.retry_count", { n: account.retry_count })}</div>
          )}
          {lastSeen && <div className="text-[11.5px] text-faint mt-1">{t("ilink.last_event", { time: lastSeen })}</div>}
          {account.last_error && <div className="text-[11.5px] text-danger mt-1">{account.last_error}</div>}
        </div>
        <div className="flex flex-col items-end gap-1.5 shrink-0">
          {!account.default && (
            <button
              className="text-[12px] text-muted hover:text-ink"
              data-testid={`ilink-default-${account.account_id}`}
              onClick={async () => {
                const result = await setDefaultWeChatIlinkAccount(account.account_id);
                if (result.ok) onChanged();
              }}
            >
              {t("conn.make_default")}
            </button>
          )}
          {(account.needs_reauth || account.state === "auth_required" || account.state === "failed") && (
            <button className="text-[12px] text-accent" data-testid={`ilink-reauth-${account.account_id}`} onClick={onReauth}>
              {t("ilink.reauth")}
            </button>
          )}
          <button
            className="text-[12px] text-danger/80 hover:text-danger"
            data-testid={`ilink-disconnect-${account.account_id}`}
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              const result = await disconnectWeChatIlinkAccount(account.account_id);
              setBusy(false);
              if (result.ok) onChanged();
            }}
          >
            {busy ? t("ilink.disconnecting") : t("conn.disconnect")}
          </button>
        </div>
      </div>
      <AllowlistBlock
        c={c}
        teamId={account.account_id}
        allowed={account.allowed_users}
        allowedNames={account.allowed_user_names}
        onChanged={onChanged}
      />
      <UnauthorizedBlock c={c} teamId={account.account_id} onChanged={onChanged} />
    </section>
  );
}

function QrDialog({
  cTitle,
  accountId,
  onClose,
  onConfirmed,
}: {
  cTitle: string;
  accountId?: string;
  onClose: () => void;
  onConfirmed: () => void;
}) {
  const { t } = useT();
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40" data-testid="ilink-qr-dialog">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <div className="absolute left-1/2 top-[10%] -translate-x-1/2 w-[440px] max-w-[calc(100vw-2rem)] bg-panel rounded-2xl border border-line shadow-2xl" role="dialog" aria-label={t(accountId ? "ilink.reauth_title" : "conn.connect_title", { title: cTitle })}>
        <div className="flex items-center px-5 pt-5">
          <h3 className="font-semibold text-[16px] flex-1">{t(accountId ? "ilink.reauth_title" : "conn.connect_title", { title: cTitle })}</h3>
          <button className="text-faint hover:text-ink text-[18px]" title={t("conn.close")} onClick={onClose}>×</button>
        </div>
        <WeChatIlinkQrFlow accountId={accountId} onConfirmed={onConfirmed} />
      </div>
    </div>
  );
}

function aggregate(accounts: WeChatIlinkAccount[]): string {
  if (accounts.some((a) => a.state === "live")) return "live";
  if (accounts.some((a) => a.state === "reconnecting" || a.state === "connecting")) return "reconnecting";
  if (accounts.some((a) => a.state === "auth_required" || a.needs_reauth)) return "auth_required";
  return "offline";
}

function stateDot(state: string): string {
  const color = state === "live" ? "bg-ok" : state === "reconnecting" || state === "connecting" ? "bg-warnInk" : state === "auth_required" || state === "failed" ? "bg-danger" : "bg-muted";
  return `w-2 h-2 rounded-full shrink-0 ${color}`;
}

function stateTag(state: string): string {
  if (state === "live") return TAG_QUIET;
  if (state === "reconnecting" || state === "connecting") return TAG_WARN;
  if (state === "auth_required" || state === "failed") return "text-[10.5px] font-semibold px-1.5 py-0.5 rounded bg-danger/10 text-danger shrink-0";
  return TAG_QUIET;
}
