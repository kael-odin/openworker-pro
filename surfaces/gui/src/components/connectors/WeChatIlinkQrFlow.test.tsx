import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "../../test-utils";
import { WeChatIlinkQrFlow } from "./WeChatIlinkQrFlow";

const api = vi.hoisted(() => ({
  cancel: vi.fn(),
  create: vi.fn(),
  get: vi.fn(),
  reauth: vi.fn(),
}));

vi.mock("../../api", async () => {
  const actual = await vi.importActual<typeof import("../../api")>("../../api");
  return {
    ...actual,
    cancelWeChatIlinkQr: api.cancel,
    createWeChatIlinkQr: api.create,
    getWeChatIlinkQr: api.get,
    reauthWeChatIlinkAccount: api.reauth,
  };
});

vi.mock("qrcode", () => ({
  default: { toCanvas: vi.fn(() => Promise.resolve()) },
}));

beforeEach(() => {
  vi.useFakeTimers();
  api.cancel.mockReset().mockResolvedValue({ ok: true });
  api.create.mockReset().mockResolvedValue({
    ok: true,
    attempt_id: "public-attempt",
    status: "waiting",
    qr_content: "https://qr.example/scannable",
  });
  api.get.mockReset();
  api.reauth.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));

describe("WeChatIlinkQrFlow", () => {
  it("polls serially and confirms without exposing backend credentials", async () => {
    let resolvePoll!: (value: unknown) => void;
    api.get.mockImplementation(
      () => new Promise((resolve) => { resolvePoll = resolve; }),
    );
    const confirmed = vi.fn();
    render(<WeChatIlinkQrFlow onConfirmed={confirmed} />);
    await act(async () => undefined);

    expect(screen.getByTestId("ilink-qr-image")).not.toBeNull();
    await advance(1500);
    expect(api.get).toHaveBeenCalledTimes(1);
    await advance(10_000);
    expect(api.get).toHaveBeenCalledTimes(1);

    await act(async () => resolvePoll({
      ok: true,
      attempt_id: "public-attempt",
      status: "confirmed",
      account_id: "Account-A",
      display_name: "My WeChat",
    }));
    expect(confirmed).toHaveBeenCalledWith("Account-A");
    expect(document.body.textContent).not.toContain("bot_token");
    expect(document.body.textContent).not.toContain("raw-poll");
  });

  it("cancels the backend attempt when unmounted", async () => {
    api.get.mockResolvedValue({ ok: true, attempt_id: "public-attempt", status: "waiting" });
    const view = render(<WeChatIlinkQrFlow onConfirmed={() => undefined} />);
    await act(async () => undefined);
    view.unmount();
    await act(async () => undefined);
    expect(api.cancel).toHaveBeenCalledWith("public-attempt");
  });

  it("binds reauthentication to the selected account", async () => {
    api.reauth.mockResolvedValue({
      ok: true,
      attempt_id: "reauth-attempt",
      status: "waiting",
      qr_content: "https://qr.example/reauth",
    });
    render(<WeChatIlinkQrFlow accountId="Account-A" onConfirmed={() => undefined} />);
    await act(async () => undefined);
    expect(api.reauth).toHaveBeenCalledWith("Account-A");
    expect(api.create).not.toHaveBeenCalled();
  });
});
