import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../test-utils";
import { NotifyChannelsSection } from "./NotifyChannelsSection";

const api = vi.hoisted(() => ({
  getNotifyChannels: vi.fn(),
  getNotifyChannelConfig: vi.fn(),
  saveNotifyChannelConfig: vi.fn(),
  setNotifyChannelEnabled: vi.fn(),
  testNotifyChannel: vi.fn(),
}));

vi.mock("../api", () => api);

beforeEach(() => {
  api.getNotifyChannels.mockResolvedValue({
    channels: [
      {
        channel: "dingtalk",
        label: "钉钉",
        description: "webhook",
        enabled: true,
        configured: true,
      },
    ],
  });
  api.getNotifyChannelConfig.mockResolvedValue({
    channel: "dingtalk",
    config: { enabled: true, url: "••••••", secret: "••••••" },
  });
  api.saveNotifyChannelConfig.mockResolvedValue({ ok: true });
  api.setNotifyChannelEnabled.mockResolvedValue({ ok: true });
  api.testNotifyChannel.mockResolvedValue({
    ok: true,
    results: [{ ok: true, channel: "dingtalk", error: null }],
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("NotifyChannelsSection secret-preserving API usage", () => {
  it("toggles enabled without posting an unloaded config", async () => {
    render(<NotifyChannelsSection />);
    expect(await screen.findByText("钉钉")).toBeTruthy();
    fireEvent.click(screen.getByText("钉钉"));
    const toggle = await screen.findByRole("checkbox");
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(api.setNotifyChannelEnabled).toHaveBeenCalledWith("dingtalk", false);
    });
    expect(api.saveNotifyChannelConfig).not.toHaveBeenCalled();
  });

  it("does not send masked secrets in save or test patches", async () => {
    render(<NotifyChannelsSection />);
    fireEvent.click(await screen.findByText("钉钉"));
    await waitFor(() => expect(api.getNotifyChannelConfig).toHaveBeenCalled());

    fireEvent.click(screen.getByText("保存"));
    await waitFor(() => {
      expect(api.saveNotifyChannelConfig).toHaveBeenCalledWith("dingtalk", {});
    });
    expect(api.saveNotifyChannelConfig.mock.calls[0][1]).not.toHaveProperty("enabled");

    fireEvent.click(screen.getByText("测试发送"));
    await waitFor(() => {
      expect(api.testNotifyChannel).toHaveBeenCalledWith("dingtalk", {});
    });
  });
});
