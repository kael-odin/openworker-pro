// SchedulePicker — 数字人定时调度的可视化编辑器（批次 D2）。
// 深度复刻 halo SchedulePicker：interval pill 快速档(5m/15m/30m/1h/6h/12h/1d/7d)
// + cron grid 编辑器(daily/weekly/monthly + weekday 按钮 + month-day grid + HourPicker 6×4
// + MinutePicker 6×10 popover) + 中文可读预览(每天 00:00 / 每周一 09:30 …)。
// 不引 cronstrue 库（halo 用了但中文支持差，自己写简单 if/else 拼）。
import { useState } from "react";
import { useT } from "../../i18n/I18nProvider";

const PILL =
  "text-[12px] px-2.5 py-1 rounded-full border transition-colors shrink-0";

type Mode = "interval" | "cron";

const INTERVALS: Array<{ label: string; cron: string }> = [
  { label: "5m", cron: "*/5 * * * *" },
  { label: "15m", cron: "*/15 * * * *" },
  { label: "30m", cron: "*/30 * * * *" },
  { label: "1h", cron: "0 * * * *" },
  { label: "6h", cron: "0 */6 * * *" },
  { label: "12h", cron: "0 */12 * * *" },
  { label: "1d", cron: "0 0 * * *" },
  { label: "7d", cron: "0 0 * * 0" },
];

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

// 把 cron 5 字段拆成可编辑的 parts：minute, hour, day-of-month, day-of-week。
function parseCron(cron: string) {
  const parts = cron.trim().split(/\s+/);
  return {
    minute: parts[0] || "*",
    hour: parts[1] || "*",
    dom: parts[2] || "*",
    dow: parts[3] || "*",
    month: parts[4] || "*",
  };
}

// 检测一个 cron 是否匹配某个 interval 预设。
function matchInterval(cron: string): string | null {
  const found = INTERVALS.find((i) => i.cron === cron.trim());
  return found ? found.cron : null;
}

export function SchedulePicker({
  cron,
  onChange,
}: {
  cron: string;
  onChange: (cron: string) => void;
}) {
  const { t } = useT();
  const preset = matchInterval(cron);
  // 如果当前 cron 是预设或全星（未配置），默认 interval 模式；否则 cron 模式。
  const [mode, setMode] = useState<Mode>(preset || cron.trim() === "* * * * *" ? "interval" : "cron");

  const parts = parseCron(cron);

  const setCronParts = (p: Partial<ReturnType<typeof parseCron>>) => {
    const merged = { ...parts, ...p };
    onChange(`${merged.minute} ${merged.hour} ${merged.dom} ${merged.dow} ${merged.month}`);
  };

  return (
    <div>
      {/* mode toggle */}
      <div className="flex gap-1.5 mb-3">
        <button
          className={PILL + (mode === "interval" ? " bg-accent text-white border-accent" : " bg-paper border-line text-muted")}
          onClick={() => setMode("interval")}
        >
          {t("dh_edit.sched_interval")}
        </button>
        <button
          className={PILL + (mode === "cron" ? " bg-accent text-white border-accent" : " bg-paper border-line text-muted")}
          onClick={() => setMode("cron")}
        >
          {t("dh_edit.sched_custom")}
        </button>
      </div>

      {mode === "interval" ? (
        <div className="flex flex-wrap gap-1.5">
          {INTERVALS.map((i) => (
            <button
              key={i.label}
              className={PILL + (preset === i.cron ? " bg-accent text-white border-accent" : " bg-paper border-line text-muted hover:border-lineStrong")}
              onClick={() => onChange(i.cron)}
            >
              {i.label}
            </button>
          ))}
        </div>
      ) : (
        <CronEditor parts={parts} setCronParts={setCronParts} />
      )}

      {/* human-readable preview */}
      <div className="mt-3 text-[12px] text-muted">
        {t("dh_edit.sched_preview")}: {cronHumanZh(cron)}
      </div>
    </div>
  );
}

function CronEditor({
  parts,
  setCronParts,
}: {
  parts: ReturnType<typeof parseCron>;
  setCronParts: (p: Partial<ReturnType<typeof parseCron>>) => void;
}) {
  const { t } = useT();
  // 频率：daily / weekly / monthly。根据当前 dom/dow 推断。
  const freq = parts.dow !== "*" ? "weekly" : parts.dom !== "*" ? "monthly" : "daily";
  const [freqState, setFreqState] = useState(freq);

  const setFreq = (f: "daily" | "weekly" | "monthly") => {
    setFreqState(f);
    if (f === "daily") setCronParts({ dom: "*", dow: "*" });
    else if (f === "weekly") setCronParts({ dom: "*", dow: "1" });
    else setCronParts({ dom: "1", dow: "*" });
  };

  // weekday toggle (1-7, Mon-Sun, cron dow where 0/7=Sun)
  const selectedDows = parts.dow === "*" ? [] : parts.dow.split(",").map((s) => parseInt(s, 10));
  const toggleDow = (idx: number) => {
    // idx 0..6, cron dow: 1-6 Mon-Sat, 0/7 Sun
    const cronVal = idx === 6 ? 0 : idx + 1;
    const next = selectedDows.includes(cronVal)
      ? selectedDows.filter((d) => d !== cronVal)
      : [...selectedDows, cronVal];
    setCronParts({ dow: next.length ? next.join(",") : "*" });
  };

  // month-day toggle (1-31)
  const selectedDoms = parts.dom === "*" ? [] : parts.dom.split(",").map((s) => parseInt(s, 10));
  const toggleDom = (day: number) => {
    const next = selectedDoms.includes(day)
      ? selectedDoms.filter((d) => d !== day)
      : [...selectedDoms, day];
    setCronParts({ dom: next.length ? next.join(",") : "*" });
  };

  const selectedHours = parts.hour === "*" ? [] : parts.hour.split(",").map((s) => parseInt(s, 10));
  const toggleHour = (h: number) => {
    const next = selectedHours.includes(h)
      ? selectedHours.filter((x) => x !== h)
      : [...selectedHours, h];
    setCronParts({ hour: next.length ? next.sort((a, b) => a - b).join(",") : "*" });
  };

  return (
    <div className="space-y-3">
      {/* frequency tabs */}
      <div className="flex gap-1.5">
        {(["daily", "weekly", "monthly"] as const).map((f) => (
          <button
            key={f}
            className={PILL + (freqState === f ? " bg-accent text-white border-accent" : " bg-paper border-line text-muted")}
            onClick={() => setFreq(f)}
          >
            {t("dh_edit.sched_" + f)}
          </button>
        ))}
      </div>

      {freqState === "weekly" && (
        <div className="flex flex-wrap gap-1.5">
          {WEEKDAYS.map((label, idx) => {
            const cronVal = idx === 6 ? 0 : idx + 1;
            const active = selectedDows.includes(cronVal);
            return (
              <button
                key={label}
                className={PILL + (active ? " bg-accent text-white border-accent" : " bg-paper border-line text-muted")}
                onClick={() => toggleDow(idx)}
              >
                {label}
              </button>
            );
          })}
        </div>
      )}

      {freqState === "monthly" && (
        <div className="grid grid-cols-7 gap-1 max-w-[280px]">
          {Array.from({ length: 31 }, (_, i) => i + 1).map((day) => {
            const active = selectedDoms.includes(day);
            return (
              <button
                key={day}
                className={"text-[11px] py-1 rounded border " + (active ? "bg-accent text-white border-accent" : "bg-paper border-line text-muted")}
                onClick={() => toggleDom(day)}
              >
                {day}
              </button>
            );
          })}
        </div>
      )}

      {/* hour picker 6×4 grid (0-23) */}
      <div>
        <div className="text-[12px] text-muted mb-1.5">{t("dh_edit.sched_hour")}</div>
        <div className="grid grid-cols-6 gap-1 max-w-[280px]">
          {Array.from({ length: 24 }, (_, h) => {
            const active = selectedHours.includes(h);
            return (
              <button
                key={h}
                className={"text-[11px] py-1 rounded border " + (active ? "bg-accent text-white border-accent" : "bg-paper border-line text-muted")}
                onClick={() => toggleHour(h)}
              >
                {String(h).padStart(2, "0")}
              </button>
            );
          })}
        </div>
      </div>

      {/* minute picker — single-select popover (0,5,10,...,55) */}
      <MinutePicker
        minute={parts.minute}
        onChange={(m) => setCronParts({ minute: m })}
      />
    </div>
  );
}

function MinutePicker({ minute, onChange }: { minute: string; onChange: (m: string) => void }) {
  const { t } = useT();
  const [open, setOpen] = useState(false);
  const current = minute === "*" ? null : parseInt(minute, 10);

  return (
    <div className="relative">
      <div className="text-[12px] text-muted mb-1.5">{t("dh_edit.sched_minute")}</div>
      <button
        className="text-[12px] px-2.5 py-1 rounded-full border border-line bg-paper text-muted hover:border-lineStrong"
        onClick={() => setOpen((v) => !v)}
      >
        {current === null ? t("dh_edit.sched_min_any") : String(current).padStart(2, "0")}
      </button>
      {open && (
        <div className="absolute z-20 mt-1 p-2 rounded-lg border border-line bg-panel shadow-lg grid grid-cols-6 gap-1 max-w-[280px]">
          {Array.from({ length: 12 }, (_, i) => i * 5).map((m) => {
            const active = current === m;
            return (
              <button
                key={m}
                className={"text-[11px] py-1 rounded border " + (active ? "bg-accent text-white border-accent" : "bg-paper border-line text-muted")}
                onClick={() => {
                  onChange(String(m));
                  setOpen(false);
                }}
              >
                {String(m).padStart(2, "0")}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// 中文可读预览：把常见 cron 模式翻译成人话。不追求全覆盖，覆盖不了的回退原 cron。
function cronHumanZh(cron: string): string {
  const p = parseCron(cron);
  // interval presets
  const presets: Record<string, string> = {
    "*/5 * * * *": "每 5 分钟",
    "*/15 * * * *": "每 15 分钟",
    "*/30 * * * *": "每 30 分钟",
    "0 * * * *": "每小时整点",
    "0 */6 * * *": "每 6 小时",
    "0 */12 * * *": "每 12 小时",
    "0 0 * * *": "每天 00:00",
    "0 0 * * 0": "每周日 00:00",
  };
  if (presets[cron.trim()]) return presets[cron.trim()];

  const min = p.minute === "*" ? "00" : p.minute.padStart(2, "0");
  const hour = p.hour === "*" ? "每" : p.hour.padStart(2, "0");
  const time = `${hour === "每" ? "每小时" : hour}:${min}`;

  if (p.dow !== "*" && p.dom === "*") {
    const dows = p.dow.split(",");
    const labels = dows.map((d) => {
      const n = parseInt(d, 10);
      return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][n === 0 ? 0 : n];
    });
    return `每${labels.join("/")} ${p.hour === "*" ? "" : time}`;
  }
  if (p.dom !== "*" && p.dow === "*") {
    return `每月 ${p.dom} 日 ${time}`;
  }
  if (p.dom === "*" && p.dow === "*") {
    return `每天 ${time}`;
  }
  return cron;
}
