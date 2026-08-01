// ConfigFieldInput — shared form control for one config_schema field.
//
// Renders the right widget for each InputType (string/text/number/boolean/url/email/select +
// json/stringList/urlList/keyvalue/date/datetime). Used by both the install dialog
// (DigitalHumansSection) and the instance editor (DhConfigBlock) so the two never drift apart.
//
// List-type fields (stringList/urlList/keyvalue) share a ListEditor subcomponent that supports
// add/remove/reorder of rows. visible_if conditional display is evaluated by the caller before
// mapping (see evalCondition in cond.ts); this component only renders a field it's given.
import { useEffect, useState } from "react";
import type { ConfigField, SelectOption } from "../../api";
import type { TFunc } from "../../i18n/I18nProvider";

const INPUT =
  "px-3 py-1.5 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent w-full";
const LABEL = "text-[12px] font-medium text-muted mb-1 block";
const ROW_BTN =
  "shrink-0 px-2 py-1 rounded-md border border-line bg-paper text-[11px] text-muted hover:text-ink hover:border-accent";

export function ConfigFieldInput({
  f,
  value,
  onChange,
  t,
}: {
  f: ConfigField;
  value: unknown;
  onChange: (v: unknown) => void;
  t: TFunc;
}) {
  const isSecret = f.secret;
  const required = f.required;
  const labelSuffix = required ? " *" : "";

  let control: React.ReactNode;
  if (f.type === "boolean") {
    control = (
      <label className="flex items-center gap-2 text-[13px] text-ink cursor-pointer">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        {f.description || t("dh_edit.field_boolean")}
      </label>
    );
  } else if (f.type === "select") {
    control = f.multiple ? (
      <SelectMulti f={f} value={value} onChange={onChange} t={t} />
    ) : (
      <select
        className={INPUT}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      >
        {!f.options?.some((o: SelectOption) => String(o.value) === String(value)) && (
          <option value="">{t("dh_edit.field_select_ph")}</option>
        )}
        {f.options?.map((o: SelectOption) => (
          <option key={String(o.value)} value={String(o.value)}>
            {o.label}
          </option>
        ))}
      </select>
    );
  } else if (f.type === "text") {
    control = (
      <textarea
        className={INPUT + " min-h-[60px] resize-y"}
        placeholder={f.placeholder}
        value={String(value ?? "")}
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else if (f.type === "json") {
    control = (
      <JsonEditor f={f} value={value} onChange={onChange} t={t} />
    );
  } else if (f.type === "stringList" || f.type === "urlList") {
    control = (
      <StringListEditor f={f} value={value} onChange={onChange} t={t} isUrl={f.type === "urlList"} />
    );
  } else if (f.type === "keyvalue") {
    control = (
      <KeyValueEditor f={f} value={value} onChange={onChange} t={t} />
    );
  } else if (f.type === "date") {
    control = (
      <input
        className={INPUT}
        type="date"
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else if (f.type === "datetime") {
    control = (
      <input
        className={INPUT}
        type="datetime-local"
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else {
    // string / number / url / email — basic input
    control = (
      <input
        className={INPUT}
        type={isSecret ? "password" : f.type === "number" ? "number" : f.type === "email" ? "email" : f.type === "url" ? "url" : "text"}
        placeholder={f.placeholder}
        value={String(value ?? "")}
        spellCheck={false}
        onChange={(e) => onChange(f.type === "number" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value)}
      />
    );
  }

  return (
    <div className="mb-2.5">
      <label className={LABEL}>
        {f.label}
        {labelSuffix}
        {isSecret && <span className="text-faint ml-1">· {t("dh_edit.field_secret")}</span>}
      </label>
      {control}
      {f.description && f.type !== "boolean" && (
        <div className="text-[11px] text-faint mt-0.5">{f.description}</div>
      )}
    </div>
  );
}

// --- multi-select (checkbox group) ---
function SelectMulti({
  f,
  value,
  onChange,
  t,
}: {
  f: ConfigField;
  value: unknown;
  onChange: (v: string[]) => void;
  t: TFunc;
}) {
  const arr = Array.isArray(value) ? value.map(String) : [];
  const toggle = (v: string) => {
    if (arr.includes(v)) onChange(arr.filter((x) => x !== v));
    else onChange([...arr, v]);
  };
  return (
    <div className="flex flex-col gap-1">
      {f.options?.map((o: SelectOption) => (
        <label key={String(o.value)} className="flex items-center gap-2 text-[13px] text-ink cursor-pointer">
          <input
            type="checkbox"
            checked={arr.includes(String(o.value))}
            onChange={() => toggle(String(o.value))}
          />
          {o.label}
        </label>
      ))}
      {(!f.options || f.options.length === 0) && (
        <span className="text-[11px] text-faint">{t("dh_edit.field_no_options")}</span>
      )}
    </div>
  );
}

// --- JSON code editor with syntax validation feedback ---
function JsonEditor({
  f,
  value,
  onChange,
  t,
}: {
  f: ConfigField;
  value: unknown;
  onChange: (v: unknown) => void;
  t: TFunc;
}) {
  // Display the raw text (what the user types), validate on blur.
  const [text, setText] = useState<string>(
    typeof value === "string" ? value : value === undefined || value === null ? "" : JSON.stringify(value, null, 2),
  );
  const [err, setErr] = useState<string | null>(null);

  // Sync from parent value when it changes externally (e.g. async detail load fills defaults
  // after mount). Only re-sync when the parent's value differs from what we'd serialize — avoids
  // clobbering in-progress edits on every keystroke (which doesn't change the parent value).
  useEffect(() => {
    const expected = typeof value === "string" ? value : value === undefined || value === null ? "" : JSON.stringify(value, null, 2);
    if (expected !== text && !err) {
      setText(expected);
    }
  }, [value]);

  const validate = (raw: string) => {
    if (raw.trim() === "") {
      onChange(undefined);
      setErr(null);
      return;
    }
    try {
      const parsed = JSON.parse(raw);
      onChange(parsed);
      setErr(null);
    } catch (e) {
      setErr(t("dh_edit.field_json_invalid") + (e instanceof Error ? ": " + e.message : ""));
    }
  };

  return (
    <div>
      <textarea
        className={INPUT + " min-h-[80px] resize-y font-mono text-[12px]"}
        placeholder={f.placeholder || t("dh_edit.field_json_ph")}
        value={text}
        spellCheck={false}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => validate(text)}
      />
      {err && <div className="text-[11px] text-red-500 mt-0.5">{err}</div>}
    </div>
  );
}

// --- list-of-strings editor (stringList / urlList) ---
function StringListEditor({
  value,
  onChange,
  t,
  isUrl,
}: {
  f: ConfigField;
  value: unknown;
  onChange: (v: string[]) => void;
  t: TFunc;
  isUrl: boolean;
}) {
  const arr = Array.isArray(value)
    ? value.map(String)
    : typeof value === "string"
      ? value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
      : [];

  const update = (i: number, v: string) => {
    const next = [...arr];
    next[i] = v;
    onChange(next);
  };
  const add = () => onChange([...arr, ""]);
  const remove = (i: number) => onChange(arr.filter((_, idx) => idx !== i));
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= arr.length) return;
    const next = [...arr];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };

  return (
    <div className="flex flex-col gap-1.5">
      {arr.map((item, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <input
            className={INPUT}
            type={isUrl ? "url" : "text"}
            placeholder={isUrl ? "https://" : ""}
            value={item}
            spellCheck={false}
            onChange={(e) => update(i, e.target.value)}
          />
          <button type="button" className={ROW_BTN} onClick={() => move(i, -1)} title={t("dh_edit.list_up")}>↑</button>
          <button type="button" className={ROW_BTN} onClick={() => move(i, 1)} title={t("dh_edit.list_down")}>↓</button>
          <button type="button" className={ROW_BTN} onClick={() => remove(i)} title={t("dh_edit.list_remove")}>✕</button>
        </div>
      ))}
      <button type="button" className={ROW_BTN + " self-start"} onClick={add}>
        + {t("dh_edit.list_add")}
      </button>
    </div>
  );
}

// --- key/value pair editor (keyvalue) ---
function KeyValueEditor({
  value,
  onChange,
  t,
}: {
  f: ConfigField;
  value: unknown;
  onChange: (v: { key: string; value: unknown }[]) => void;
  t: TFunc;
}) {
  // Accept both [{key, value}] arrays and {k: v} objects.
  const rows: { key: string; value: string }[] = Array.isArray(value)
    ? (value as { key?: string; value?: unknown }[]).map((r) => ({ key: String(r?.key ?? ""), value: String(r?.value ?? "") }))
    : value && typeof value === "object"
      ? Object.entries(value as Record<string, unknown>).map(([k, v]) => ({ key: k, value: String(v) }))
      : [];

  const update = (i: number, field: "key" | "value", v: string) => {
    const next = rows.map((r, idx) => (idx === i ? { ...r, [field]: v } : r));
    onChange(next.map((r) => ({ key: r.key, value: r.value })));
  };
  const add = () => onChange([...rows.map((r) => ({ key: r.key, value: r.value })), { key: "", value: "" }]);
  const remove = (i: number) => onChange(rows.map((r) => ({ key: r.key, value: r.value })).filter((_, idx) => idx !== i));

  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((row, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <input
            className={INPUT + " flex-1"}
            placeholder={t("dh_edit.field_kv_key_ph")}
            value={row.key}
            spellCheck={false}
            onChange={(e) => update(i, "key", e.target.value)}
          />
          <input
            className={INPUT + " flex-1"}
            placeholder={t("dh_edit.field_kv_val_ph")}
            value={row.value}
            spellCheck={false}
            onChange={(e) => update(i, "value", e.target.value)}
          />
          <button type="button" className={ROW_BTN} onClick={() => remove(i)} title={t("dh_edit.list_remove")}>✕</button>
        </div>
      ))}
      <button type="button" className={ROW_BTN + " self-start"} onClick={add}>
        + {t("dh_edit.list_add")}
      </button>
    </div>
  );
}
