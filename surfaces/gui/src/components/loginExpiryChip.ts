// loginExpiryChip — renders a compact expiry/status chip for a browser login session.
// Shared by CustomizeView's Logins tab and DhEditPanel's login health block.
//
// The backend's inspect_login_expiry() returns {status, expires_at?} where status is one
// of no_state / session / valid / expiring / expired. We turn that into a colored chip:
//   expired  → danger
//   expiring → warn (amber)
//   valid    → ok (green) + "有效期至 {date}"
//   session  → faint ("会话型" — only session cookies, no expiry signal)
//   no_state → nothing (the row already shows a "待登录" chip elsewhere)
import type { LoginExpiry } from "../api";
import type { TFn } from "./CustomizeView";

export function loginExpiryChip(
  expiry: LoginExpiry | undefined,
  t: TFn,
): { text: string; cls: string } | null {
  if (!expiry) return null;
  const date = expiry.expires_at ? expiry.expires_at.slice(0, 10) : "";
  switch (expiry.status) {
    case "expired":
      return { text: t("customize.logins_expiry_expired"), cls: "bg-dangerSoft text-danger" };
    case "expiring":
      return { text: t("customize.logins_expiry_expiring"), cls: "bg-warnSoft text-warnInk" };
    case "valid":
      return { text: t("customize.logins_expiry_valid", { date }), cls: "bg-okSoft text-ok" };
    case "session":
      return { text: t("customize.logins_expiry_session"), cls: "border border-line text-faint" };
    default:
      return null;
  }
}
