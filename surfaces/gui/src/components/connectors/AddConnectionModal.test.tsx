import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "../../test-utils";
import type { Connector } from "../../api";
import { AddConnectionModal } from "./AddConnectionModal";

const qr = vi.hoisted(() => ({ rendered: vi.fn() }));

vi.mock("./WeChatIlinkQrFlow", () => ({
  WeChatIlinkQrFlow: () => {
    qr.rendered();
    return <div data-testid="mock-ilink-qr">QR only</div>;
  },
}));

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
  connected: false,
  account: null,
  enabled: false,
  brand_color: "#07c160",
  logo: "wechat_ilink",
  allowed_users: [],
  tools: [],
  managed: false,
  managed_profile: false,
};

beforeEach(() => qr.rendered.mockClear());
afterEach(cleanup);

describe("AddConnectionModal personal WeChat", () => {
  it("always uses the dedicated QR flow instead of the generic secret form", () => {
    const close = vi.fn();
    render(
      <AddConnectionModal
        c={connector}
        cloud={null}
        onClose={close}
        onChanged={() => undefined}
      />,
    );
    expect(screen.getByTestId("mock-ilink-qr")).not.toBeNull();
    expect(qr.rendered).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("手动")).toBeNull();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(close).toHaveBeenCalledTimes(1);
  });
});
