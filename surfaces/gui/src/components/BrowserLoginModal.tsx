// BrowserLoginModal — capture flow for a persisted browser login session (E5).
//
// Two paths, mirroring coworker/browser_login_capture.py:
//   - Playwright headed (preferred): captureBrowserLogin opens a visible chromium window
//     at the login URL; the user logs in, then clicks "I'm logged in" →
//     confirmBrowserLoginCapture dumps storageState to disk.
//   - Cookie paste (fallback): a textarea takes a cookie JSON array → saveBrowserLoginCookies.
//     Used when the sidecar reports Playwright unavailable (captureBrowserLogin returns
//     {fallback: "cookies"}), or when the user picks it explicitly.
//
// Shared by CustomizeView's Logins tab (add/re-login) and DhEditPanel's login health block
// (one-click login for a missing site). `existing` is non-null when re-logging-in.
import { useState } from "react";
import {
  addBrowserLogin,
  cancelBrowserLoginCapture,
  captureBrowserLogin,
  confirmBrowserLoginCapture,
  saveBrowserLoginCookies,
  type BrowserLogin,
} from "../api";
import { Icon } from "./Icon";
import { useT } from "../i18n/I18nProvider";

const INPUT =
  "px-2.5 py-1.5 rounded-md border border-line bg-paper text-[12.5px] text-ink outline-none focus:border-accent min-w-0";
const BTN_PRIMARY =
  "text-[12px] px-2.5 py-1.5 rounded-md bg-accent text-white hover:opacity-90";

export function BrowserLoginModal({
  existing,
  presetUrl,
  presetLabel,
  onClose,
  onSaved,
}: {
  existing: BrowserLogin | null;
  // Pre-fill url/label when launched from a "log in to this site" affordance (DhEditPanel).
  presetUrl?: string;
  presetLabel?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useT();
  const [url, setUrl] = useState(existing?.url ?? presetUrl ?? "");
  const [label, setLabel] = useState(existing?.label ?? presetLabel ?? "");
  const [mode, setMode] = useState<"playwright" | "cookies">(
    existing?.mode === "cookies" ? "cookies" : "playwright",
  );
  const [cookieJson, setCookieJson] = useState("");
  // Flow state: "form" → "capturing" (Playwright window open) → done.
  const [flow, setFlow] = useState<"form" | "capturing">("form");
  const [loginId, setLoginId] = useState<string>(existing?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const startPlaywright = async () => {
    setBusy(true);
    setError("");
    try {
      // Ensure the login entry exists before capturing.
      let id = loginId;
      if (!id) {
        const r = await addBrowserLogin(url.trim(), label.trim() || url.trim(), "playwright");
        if (!r.ok || !r.entry) {
          setError(r.error || t("customize.logins_capture_failed", { error: "" }));
          setBusy(false);
          return;
        }
        id = r.entry.id;
        setLoginId(id);
        // If the backend already tells us Playwright is unavailable, skip the doomed
        // capture call and switch straight to cookie-paste mode.
        if (r.playwright_available === false) {
          setMode("cookies");
          setError(t("customize.logins_playwright_unavailable"));
          setBusy(false);
          return;
        }
      }
      const cap = await captureBrowserLogin(id);
      if (cap.ok) {
        setMode("playwright");
        setFlow("capturing");
      } else if (cap.fallback === "cookies") {
        // Playwright unavailable — switch to cookie paste.
        setMode("cookies");
        setError(t("customize.logins_playwright_unavailable"));
      } else {
        setError(cap.error || t("customize.logins_capture_failed", { error: "" }));
      }
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
    setBusy(false);
  };

  const confirmCapture = async () => {
    if (!loginId) return;
    setBusy(true);
    setError("");
    try {
      const r = await confirmBrowserLoginCapture(loginId);
      if (r.ok) {
        onSaved();
      } else {
        setError(r.error || t("customize.logins_capture_failed", { error: "" }));
      }
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
    setBusy(false);
  };

  const cancelCapture = async () => {
    setBusy(true);
    try {
      await cancelBrowserLoginCapture();
    } catch {
      /* ignore */
    }
    setBusy(false);
    setFlow("form");
  };

  const saveCookies = async () => {
    setBusy(true);
    setError("");
    try {
      let id = loginId;
      if (!id) {
        const r = await addBrowserLogin(url.trim(), label.trim() || url.trim(), "cookies");
        if (!r.ok || !r.entry) {
          setError(r.error || t("customize.logins_capture_failed", { error: "" }));
          setBusy(false);
          return;
        }
        id = r.entry.id;
        setLoginId(id);
      }
      const r = await saveBrowserLoginCookies(id, cookieJson);
      if (r.ok) {
        onSaved();
      } else {
        setError(r.error || t("customize.logins_cookie_invalid"));
      }
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
    setBusy(false);
  };

  const canSubmit = url.trim().length > 0;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6"
      onClick={() => {
        // Don't close silently while a Playwright window is open — cancel it first.
        if (flow === "capturing") {
          cancelCapture();
        }
        onClose();
      }}
    >
      <div
        className="bg-panel rounded-xl border border-line shadow-2xl max-w-lg w-full flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-4 py-3 border-b border-line">
          <Icon name="key" size={15} className="text-accent" />
          <span className="text-[13px] font-medium text-ink">
            {existing ? t("customize.logins_relogin") : t("customize.logins_add")}
          </span>
          <button
            className="ml-auto text-faint hover:text-ink p-1"
            onClick={() => {
              if (flow === "capturing") cancelCapture();
              onClose();
            }}
            aria-label={t("common.close")}
          >
            <Icon name="x" size={15} />
          </button>
        </div>

        <div className="px-4 py-4 space-y-3">
          {/* Step 1 fields — hidden once capturing */}
          {flow === "form" && (
            <>
              <div>
                <label className="text-[11.5px] text-faint">URL</label>
                <input
                  className={INPUT + " w-full mt-1"}
                  placeholder={t("customize.logins_url_ph")}
                  value={url}
                  spellCheck={false}
                  disabled={!!existing}
                  onChange={(e) => setUrl(e.target.value)}
                />
              </div>
              <div>
                <label className="text-[11.5px] text-faint">{t("customize.logins_label_ph")}</label>
                <input
                  className={INPUT + " w-full mt-1"}
                  placeholder={t("customize.logins_label_ph")}
                  value={label}
                  spellCheck={false}
                  onChange={(e) => setLabel(e.target.value)}
                />
              </div>
              <div>
                <label className="text-[11.5px] text-faint">{t("customize.logins_mode")}</label>
                <div className="flex gap-2 mt-1">
                  <button
                    className={
                      "flex-1 text-[12px] px-2.5 py-1.5 rounded-md border " +
                      (mode === "playwright"
                        ? "border-accent bg-accentSoft text-accent"
                        : "border-line bg-paper text-muted hover:border-lineStrong")
                    }
                    onClick={() => setMode("playwright")}
                  >
                    {t("customize.logins_mode_playwright")}
                  </button>
                  <button
                    className={
                      "flex-1 text-[12px] px-2.5 py-1.5 rounded-md border " +
                      (mode === "cookies"
                        ? "border-accent bg-accentSoft text-accent"
                        : "border-line bg-paper text-muted hover:border-lineStrong")
                    }
                    onClick={() => setMode("cookies")}
                  >
                    {t("customize.logins_mode_cookies")}
                  </button>
                </div>
              </div>
            </>
          )}

          {/* Playwright capturing state */}
          {flow === "capturing" && (
            <div className="space-y-2">
              <div className="text-[12.5px] text-ink">{t("customize.logins_capturing")}</div>
              <div className="text-[11.5px] text-faint leading-relaxed">
                {t("customize.logins_capture_hint")}
              </div>
            </div>
          )}

          {/* Cookie paste area */}
          {flow === "form" && mode === "cookies" && (
            <div>
              <label className="text-[11.5px] text-faint">{t("customize.logins_paste_cookies")}</label>
              <textarea
                className={INPUT + " w-full mt-1 font-mono"}
                rows={6}
                placeholder={t("customize.logins_cookies_ph")}
                value={cookieJson}
                spellCheck={false}
                onChange={(e) => setCookieJson(e.target.value)}
              />
              <div className="text-[11px] text-faint mt-1 leading-relaxed">
                {t("customize.logins_cookies_help")}
              </div>
            </div>
          )}

          {error && (
            <div className="text-[11.5px] text-danger leading-relaxed">{error}</div>
          )}
        </div>

        {/* Footer actions */}
        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-line">
          {flow === "capturing" ? (
            <>
              <button
                className="text-[12px] px-2.5 py-1.5 rounded-md border border-lineStrong bg-panel hover:border-lineStrong"
                disabled={busy}
                onClick={cancelCapture}
              >
                {t("customize.logins_cancel_capture")}
              </button>
              <button
                className={BTN_PRIMARY + " disabled:opacity-50"}
                disabled={busy}
                onClick={confirmCapture}
              >
                {busy ? "…" : t("customize.logins_confirm_logged_in")}
              </button>
            </>
          ) : mode === "playwright" ? (
            <>
              <button
                className="text-[12px] px-2.5 py-1.5 rounded-md border border-lineStrong bg-panel hover:border-lineStrong"
                onClick={onClose}
              >
                {t("common.cancel")}
              </button>
              <button
                className={BTN_PRIMARY + " disabled:opacity-50"}
                disabled={busy || !canSubmit}
                onClick={startPlaywright}
              >
                {busy ? "…" : t("customize.logins_start_capture")}
              </button>
            </>
          ) : (
            <>
              <button
                className="text-[12px] px-2.5 py-1.5 rounded-md border border-lineStrong bg-panel hover:border-lineStrong"
                onClick={onClose}
              >
                {t("common.cancel")}
              </button>
              <button
                className={BTN_PRIMARY + " disabled:opacity-50"}
                disabled={busy || !canSubmit || !cookieJson.trim()}
                onClick={saveCookies}
              >
                {busy ? "…" : t("customize.logins_save_cookies")}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
