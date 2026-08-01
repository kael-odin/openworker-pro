import { afterEach, expect, it, vi } from "vitest";
import { Session } from "./api";

// P1-06: the session WebSocket must auto-reconnect on unexpected close with exponential
// backoff + jitter, and NOT reconnect after an intentional close().
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;
  readyState = FakeWebSocket.CONNECTING;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn(() => {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  });

  // The Session assigns these handlers in its open(); tests drive them.
  fireOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  fireClose() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  constructor(
    public readonly url: string,
    public readonly protocols?: string | string[],
  ) {}
}

it("reconnects after an unexpected close with backoff", () => {
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("__COWORKER_API_TOKEN__", "t");

  const created: FakeWebSocket[] = [];
  const orig = FakeWebSocket;
  vi.stubGlobal("WebSocket", vi.fn((url: string, protocols?: string | string[]) => {
    const ws = new orig(url, protocols);
    created.push(ws);
    return ws;
  }));

  const onOpen = vi.fn();
  const onClose = vi.fn();
  new Session("s1", "/w", "code", { onEvent: vi.fn(), onOpen, onClose });

  // First socket connects.
  created[0].fireOpen();
  expect(onOpen).toHaveBeenCalledTimes(1);
  expect(created).toHaveLength(1);

  // Unexpected close → onClose fires, reconnect is scheduled.
  created[0].fireClose();
  expect(onClose).toHaveBeenCalledTimes(1);

  // A reconnect is scheduled; advance timers to fire it (backoff ~1s ± jitter).
  vi.advanceTimersByTime(2000);
  expect(created).toHaveLength(2);
  // The new socket has not opened yet — onOpen not re-fired until it does.
  expect(onOpen).toHaveBeenCalledTimes(1);
  created[1].fireOpen();
  expect(onOpen).toHaveBeenCalledTimes(2);
});

it("does NOT reconnect after an intentional close()", () => {
  vi.useFakeTimers();
  const created: FakeWebSocket[] = [];
  const orig = FakeWebSocket;
  vi.stubGlobal("WebSocket", vi.fn((url: string, protocols?: string | string[]) => {
    const ws = new orig(url, protocols);
    created.push(ws);
    return ws;
  }));

  const session = new Session("s1", "/w", "code", { onEvent: vi.fn() });
  created[0].fireOpen();
  session.close();

  const countAfterClose = created.length;
  // Advancing timers must not spawn a new socket.
  vi.advanceTimersByTime(60_000);
  expect(created).toHaveLength(countAfterClose);
});

it("backoff increases across consecutive failures", () => {
  vi.useFakeTimers();
  const created: FakeWebSocket[] = [];
  const orig = FakeWebSocket;
  vi.stubGlobal("WebSocket", vi.fn((url: string, protocols?: string | string[]) => {
    const ws = new orig(url, protocols);
    created.push(ws);
    return ws;
  }));

  new Session("s1", "/w", "code", { onEvent: vi.fn() });
  created[0].fireClose();

  // First reconnect: base ~1s. Fire immediately on schedule, then close again.
  vi.advanceTimersByTime(2000);
  expect(created).toHaveLength(2);
  created[1].fireClose();

  // Second reconnect: backoff ~2s. A 1s advance must NOT yet fire it.
  vi.advanceTimersByTime(1000);
  expect(created).toHaveLength(2);
  vi.advanceTimersByTime(3000);
  expect(created).toHaveLength(3);
});
