import { useCallback, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import {
  cancelWeChatIlinkQr,
  createWeChatIlinkQr,
  getWeChatIlinkQr,
  reauthWeChatIlinkAccount,
  type WeChatIlinkQrAttempt,
} from "../../api";
import { useT } from "../../i18n/I18nProvider";
import { PILL_ACCENT, PILL_LINE } from "./ui";

function QrCanvas({ value, alt }: { value: string; alt: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const target = canvas.current;
    if (!target || !value) return;
    QRCode.toCanvas(target, value, {
      width: 200,
      margin: 1,
      color: { dark: "#000000", light: "#ffffff" },
    }).catch(() => undefined);
  }, [value]);
  return (
    <canvas
      ref={canvas}
      width={200}
      height={200}
      aria-label={alt}
      role="img"
      className="w-[200px] h-[200px] rounded-lg bg-white"
      data-testid="ilink-qr-image"
    />
  );
}

const POLL_MS = 1500;
const TERMINAL = new Set(["confirmed", "expired", "failed", "cancelled"]);

export function WeChatIlinkQrFlow({
  accountId,
  onConfirmed,
}: {
  accountId?: string;
  onConfirmed: (accountId?: string) => void;
}) {
  const { t } = useT();
  const [attempt, setAttempt] = useState<WeChatIlinkQrAttempt | null>(null);
  const [starting, setStarting] = useState(true);
  const [error, setError] = useState("");
  const attemptId = useRef("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const active = useRef(true);
  const confirmedRef = useRef(onConfirmed);
  const tRef = useRef(t);
  confirmedRef.current = onConfirmed;
  tRef.current = t;

  const stopTimer = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };

  const cancelActive = useCallback(async () => {
    stopTimer();
    const id = attemptId.current;
    attemptId.current = "";
    if (id) await cancelWeChatIlinkQr(id).catch(() => undefined);
  }, []);

  const poll = useCallback(
    async (id: string) => {
      if (!active.current || attemptId.current !== id) return;
      const next = await getWeChatIlinkQr(id).catch(() => null);
      if (!active.current || attemptId.current !== id) return;
      if (!next?.ok) {
        setError(next?.error || tRef.current("ilink.qr_poll_error"));
        return;
      }
      setAttempt(next);
      if (next.status === "confirmed") {
        attemptId.current = "";
        confirmedRef.current(next.account_id);
        return;
      }
      if (next.status && TERMINAL.has(next.status)) return;
      // Deliberately schedule only after this request completes: status polls never overlap.
      timer.current = setTimeout(() => void poll(id), POLL_MS);
    },
    [],
  );

  const start = useCallback(async () => {
    await cancelActive();
    if (!active.current) return;
    setStarting(true);
    setError("");
    setAttempt(null);
    const created = await (accountId
      ? reauthWeChatIlinkAccount(accountId)
      : createWeChatIlinkQr()
    ).catch(() => null);
    if (!active.current) return;
    setStarting(false);
    if (!created?.ok || !created.attempt_id) {
      setError(created?.error || tRef.current("ilink.qr_start_error"));
      return;
    }
    attemptId.current = created.attempt_id;
    setAttempt(created);
    timer.current = setTimeout(() => void poll(created.attempt_id!), POLL_MS);
  }, [accountId, cancelActive, poll]);

  useEffect(() => {
    active.current = true;
    void start();
    return () => {
      active.current = false;
      stopTimer();
      const id = attemptId.current;
      attemptId.current = "";
      if (id) void cancelWeChatIlinkQr(id).catch(() => undefined);
    };
  }, [start]);

  const stateKey = `ilink.qr_${attempt?.status || "waiting"}`;
  const retryable = !!error || attempt?.status === "expired" || attempt?.status === "failed" || attempt?.status === "cancelled";

  return (
    <div className="px-5 py-4 space-y-3" data-testid="ilink-qr-flow">
      <p className="text-[13px] text-muted">
        {t(accountId ? "ilink.qr_reauth_intro" : "ilink.qr_intro")}
      </p>

      <div className="rounded-xl border border-line bg-paper p-4 grid place-items-center min-h-[230px]">
        {starting ? (
          <div className="text-[13px] text-muted">{t("ilink.qr_creating")}</div>
        ) : attempt?.qr_content && !TERMINAL.has(attempt.status || "") ? (
          <QrCanvas value={attempt.qr_content} alt={t("ilink.qr_alt")} />
        ) : (
          <div className="text-[13px] text-muted text-center">{t(stateKey)}</div>
        )}
      </div>

      {!starting && !error && attempt?.status && (
        <div className="text-[12.5px] text-muted text-center" data-testid="ilink-qr-status">
          {t(stateKey)}
        </div>
      )}
      {error && (
        <div className="text-[12.5px] text-danger text-center" data-testid="ilink-qr-error">
          {error}
        </div>
      )}
      {retryable && (
        <button className={PILL_ACCENT + " w-full !py-2"} onClick={() => void start()}>
          {t("ilink.qr_retry")}
        </button>
      )}
      {!retryable && attemptId.current && (
        <button
          className={PILL_LINE + " w-full !py-2"}
          onClick={async () => {
            await cancelActive();
            if (active.current) setAttempt((value) => ({ ...(value || { ok: true }), status: "cancelled" }));
          }}
        >
          {t("ilink.qr_cancel")}
        </button>
      )}

      <p className="text-[12px] text-faint text-center">{t("ilink.qr_privacy")}</p>
    </div>
  );
}
