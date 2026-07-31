// SystemPromptEditor — 编辑数字人 system_prompt 的控件（批次 D2）。
// 用户明确抱怨「指令」文本框太小不好看，这里深度复刻 halo：内联 textarea 自伸缩
// (min 144 / max 400 px) + 全屏 dialog (60vh, 字符计数, ESC/click-away 取消, Done 自动保存)。
// 不引入 CodeMirror/Monaco（halo 也不用，纯 textarea + dialog）。
import { useEffect, useRef, useState } from "react";
import { useT } from "../../i18n/I18nProvider";
import { Icon } from "../Icon";

const INPUT =
  "px-3 py-2 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent w-full font-mono leading-relaxed";

export function SystemPromptEditor({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const { t } = useT();
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [draft, setDraft] = useState(value);

  // keep draft in sync when the external value changes (e.g. after load)
  useEffect(() => {
    setDraft(value);
  }, [value]);

  // auto-resize: grow with content up to maxHeight, then scroll.
  const autoResize = (el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 400) + "px";
  };
  useEffect(() => {
    autoResize(taRef.current);
  }, [draft]);

  const commit = (v: string) => {
    setDraft(v);
    onChange(v);
  };

  return (
    <div>
      <div className="relative">
        <textarea
          ref={taRef}
          className={INPUT}
          style={{ minHeight: 144, resize: "vertical" }}
          value={draft}
          spellCheck={false}
          placeholder={t("dh_edit.prompt_ph")}
          onChange={(e) => commit(e.target.value)}
        />
        <button
          type="button"
          onClick={() => {
            setFullscreen(true);
          }}
          className="absolute top-2 right-2 text-faint hover:text-accent p-1 rounded hover:bg-paper"
          title={t("dh_edit.fullscreen")}
        >
          <Icon name="maximize" size={16} />
        </button>
      </div>
      <div className="flex justify-end mt-1">
        <span className="text-[11px] text-faint">
          {draft.length} {t("dh_edit.chars")}
        </span>
      </div>

      {fullscreen && (
        <FullscreenEditor
          initial={draft}
          onCancel={() => setFullscreen(false)}
          onDone={(v) => {
            commit(v);
            setFullscreen(false);
          }}
        />
      )}
    </div>
  );
}

function FullscreenEditor({
  initial,
  onCancel,
  onDone,
}: {
  initial: string;
  onCancel: () => void;
  onDone: (v: string) => void;
}) {
  const { t } = useT();
  const [draft, setDraft] = useState(initial);
  const ref = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="bg-panel rounded-xl2 border border-line shadow-xl w-[80%] max-w-4xl flex flex-col">
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-line">
          <span className="text-[13px] font-semibold text-ink">{t("dh_edit.prompt_fullscreen")}</span>
          <button onClick={onCancel} className="text-faint hover:text-ink">
            <Icon name="x" size={16} />
          </button>
        </div>
        <textarea
          ref={ref}
          className="flex-1 mx-4 my-3 px-3 py-2 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent font-mono leading-relaxed resize-none"
          style={{ height: "60vh" }}
          value={draft}
          spellCheck={false}
          onChange={(e) => setDraft(e.target.value)}
        />
        <div className="flex items-center justify-between px-4 py-2.5 border-t border-line">
          <span className="text-[11px] text-faint">
            {draft.length} {t("dh_edit.chars")}
          </span>
          <div className="flex gap-2">
            <button
              className="text-[12.5px] px-3 py-1.5 rounded-full text-muted hover:text-ink"
              onClick={onCancel}
            >
              {t("common.cancel")}
            </button>
            <button
              className="text-[12.5px] font-medium px-3 py-1.5 rounded-full bg-accent text-white"
              onClick={() => onDone(draft)}
            >
              {t("dh_edit.done")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
