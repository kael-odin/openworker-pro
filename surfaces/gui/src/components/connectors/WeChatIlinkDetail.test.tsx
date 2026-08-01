import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "../../test-utils";
import type { Connector, WeChatIlinkAccount } from "../../api";
import { WeChatIlinkDetail } from "./WeChatIlinkDetail";

const api = vi.hoisted(() => ({
  disconnect: vi.fn(async () => ({ ok: true })),
  setDefault: vi.fn(async () => ({ ok: true })),
}));

vi.mock("../../api", async () => {
  const actual = await vi.importActual<typeof import("../../api")>("../../api");
  return {
    ...actual,
    disconnectWeChatIlinkAccount: api.disconnect,
    setDefaultWeChatIlinkAccount: api.setDefault,
  };
});

vi.mock("./WeChatIlinkQrFlow", () => ({
  WeChatIlinkQrFlow: ({ accountId }: { accountId?: string }) => (
    <div data-testid="mock-ilink-qr">{accountId || "new-account"}</div>
  ),
}));

const accounts: WeChatIlinkAccount[] = [
  {
    account_id: "Account-A",
    display_name: "My WeChat",
    enabled: true,
    default: true,
    allowed_users: ["friend-a"],
    allowed_user_names: { "friend-a": "Alice" },
    allow_all: false,
    needs_reauth: false,
    state: "live",
    retry_count: 0,
    last_event_at: null,
    last_error: "",
  },
  {
    account_id: "Account-B",
    display_name: "Backup WeChat",
    enabled: true,
    default: false,
    allowed_users: [],
    allow_all: false,
    needs_reauth: true,
    state: "auth_required",
    retry_count: 0,
    last_event_at: null,
    last_error: "",
  },
];

const connector: Connector = {
  name: "wechat_ilink",
  title: "个人微信",
  icon: "微",
  blurb: "Personal WeChat",
  auth: "qr",
  two_way: true,
  channels: false,
  available: true,
  fields: [],
  instructions: [],
  connected: true,
  account: "My WeChat",
  enabled: true,
  brand_color: "#07c160",
  logo: "wechat_ilink",
  allowed_users: [],
  tools: [],
  managed: false,
  managed_profile: false,
  accounts,
  status: { ok: true, state: "live", accounts },
  recent: [],
  unauthorized: [],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WeChatIlinkDetail", () => {
  it("shows isolated account state and account-scoped allow-lists", () => {
    render(<WeChatIlinkDetail c={connector} cloud={null} slack={null} onChanged={() => undefined} />);
    expect(screen.getByTestId("ilink-account-Account-A").textContent).toContain("Alice");
    expect(screen.getByTestId("ilink-account-Account-B").textContent).not.toContain("Alice");
    expect(screen.getByTestId("ilink-reauth-Account-B")).not.toBeNull();
    expect(document.body.textContent).toContain("不读取其他聊天或群聊");
  });

  it("opens account-bound reauthentication and supports default/disconnect actions", async () => {
    const changed = vi.fn();
    render(<WeChatIlinkDetail c={connector} cloud={null} slack={null} onChanged={changed} />);

    fireEvent.click(screen.getByTestId("ilink-reauth-Account-B"));
    expect(screen.getByTestId("mock-ilink-qr").textContent).toBe("Account-B");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("mock-ilink-qr")).toBeNull();

    fireEvent.click(screen.getByTestId("ilink-default-Account-B"));
    await vi.waitFor(() => expect(api.setDefault).toHaveBeenCalledWith("Account-B"));

    fireEvent.click(screen.getByTestId("ilink-disconnect-Account-A"));
    await vi.waitFor(() => expect(api.disconnect).toHaveBeenCalledWith("Account-A"));
  });
});
