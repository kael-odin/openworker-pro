// DhConfigBlock — 数字人 config_schema 的动态表单区块。
// 改用共享 ConfigFieldInput 控件库（13 种类型 + visible_if 条件显示 + schema 可增删改）。
// 保存：onChange(config) → PATCH instance.user_config；onSchemaChange(schema) → PATCH instance.config_schema_override。
import { useState } from "react";
import { useT } from "../../../i18n/I18nProvider";
import { GRP, GRP_H } from "../../connectors/ui";
import type { ConfigField, InputType } from "../../../api";
import { ConfigFieldInput } from "../ConfigFieldInput";
import { evalCondition } from "../cond";

const INPUT =
  "px-3 py-1.5 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent w-full";
const ROW_BTN =
  "shrink-0 px-2 py-1 rounded-md border border-line bg-paper text-[11px] text-muted hover:text-ink hover:border-accent";

const ALL_TYPES: InputType[] = [
  "string", "text", "number", "boolean", "url", "email", "select",
  "json", "stringList", "urlList", "keyvalue", "date", "datetime",
];

export function DhConfigBlock({
  schema,
  config,
  onChange,
  editable = false,
  onSchemaChange,
}: {
  schema: ConfigField[];
  config: Record<string, unknown>;
  onChange: (config: Record<string, unknown>) => void;
  editable?: boolean;
  onSchemaChange?: (schema: ConfigField[]) => void;
}) {
  const { t } = useT();
  const [showFieldEditor, setShowFieldEditor] = useState(false);
  if (schema.length === 0 && !editable) return null;

  const setField = (key: string, value: unknown) => {
    onChange({ ...config, [key]: value });
  };

  const visibleFields = schema.filter((f) => evalCondition(f.visible_if, config));

  const moveField = (i: number, dir: -1 | 1) => {
    if (!onSchemaChange) return;
    const j = i + dir;
    if (j < 0 || j >= schema.length) return;
    const next = [...schema];
    [next[i], next[j]] = [next[j], next[i]];
    onSchemaChange(next);
  };
  const removeField = (i: number) => {
    if (!onSchemaChange) return;
    onSchemaChange(schema.filter((_, idx) => idx !== i));
  };
  const addField = (f: ConfigField) => {
    if (!onSchemaChange) return;
    onSchemaChange([...schema, f]);
  };

  return (
    <>
      <div className={GRP_H + " flex items-center justify-between"}>
        <span>{t("dh_edit.blk_config")}</span>
        {editable && (
          <button
            type="button"
            className={ROW_BTN}
            onClick={() => setShowFieldEditor((v) => !v)}
          >
            {showFieldEditor ? t("dh_edit.schema_done") : t("dh_edit.schema_edit")}
          </button>
        )}
      </div>
      <div className={GRP + " px-4 py-3"}>
        {schema.length === 0 && showFieldEditor && (
          <div className="text-[12px] text-faint mb-2">{t("dh_edit.schema_empty_hint")}</div>
        )}
        {visibleFields.map((f) => {
          // Find the index in the full schema for move/remove (visible_if filters display only).
          const fullIdx = schema.indexOf(f);
          return (
            <div key={f.key} className="relative">
              <ConfigFieldInput
                f={f}
                value={config[f.key]}
                onChange={(v) => setField(f.key, v)}
                t={t}
              />
              {showFieldEditor && onSchemaChange && (
                <div className="absolute right-0 top-0 flex gap-1">
                  <button type="button" className={ROW_BTN} onClick={() => moveField(fullIdx, -1)} title={t("dh_edit.list_up")}>↑</button>
                  <button type="button" className={ROW_BTN} onClick={() => moveField(fullIdx, 1)} title={t("dh_edit.list_down")}>↓</button>
                  <button type="button" className={ROW_BTN} onClick={() => removeField(fullIdx)} title={t("dh_edit.schema_remove")}>✕</button>
                </div>
              )}
            </div>
          );
        })}
        {showFieldEditor && onSchemaChange && (
          <AddFieldModal onAdd={addField} existingKeys={schema.map((f) => f.key)} t={t} />
        )}
      </div>
    </>
  );
}

// Inline "add a field" form (not a modal, just a collapsible row) — pick type, fill key/label/required.
function AddFieldModal({
  onAdd,
  existingKeys,
  t,
}: {
  onAdd: (f: ConfigField) => void;
  existingKeys: string[];
  t: (k: string) => string;
}) {
  const [type, setType] = useState<InputType>("string");
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [required, setRequired] = useState(false);
  const [options, setOptions] = useState<{ label: string; value: string }[]>([]);
  const [submitAttempted, setSubmitAttempted] = useState(false);

  // key must be lowercase alnum + underscore (matches DHP slug rules, avoids YAML quote needs).
  const KEY_RE = /^[a-z][a-z0-9_]*$/;
  const keyErr =
    !key.trim() ? t("dh_edit.schema_field_key") :
    !KEY_RE.test(key.trim()) ? t("dh_edit.schema_key_format") :
    existingKeys.includes(key.trim()) ? t("dh_edit.schema_key_dup") :
    "";
  const labelErr = !label.trim() ? t("dh_edit.schema_field_label") : "";
  const optionsErr =
    type === "select" && options.filter((o) => o.label.trim() && o.value.trim()).length === 0
      ? t("dh_edit.schema_select_empty")
      : "";

  const submit = () => {
    setSubmitAttempted(true);
    if (keyErr || labelErr || optionsErr) return;
    const k = key.trim();
    onAdd({
      key: k,
      label: label.trim(),
      type,
      required,
      description: "",
      options: type === "select" ? options.filter((o) => o.label.trim() && o.value.trim()) : undefined,
    });
    // reset
    setKey(""); setLabel(""); setRequired(false); setType("string");
    setOptions([]); setSubmitAttempted(false);
  };

  const ERR = "text-[11px] text-red-500 mt-0.5";

  return (
    <div className="mt-3 pt-3 border-t border-line">
      <div className="text-[12px] font-medium text-muted mb-2">{t("dh_edit.schema_add_field")}</div>
      <div className="flex flex-col gap-2">
        <div className="flex gap-2">
          <div className="flex-1">
            <input
              className={INPUT + " w-full"}
              placeholder={t("dh_edit.schema_field_key")}
              value={key}
              spellCheck={false}
              onChange={(e) => setKey(e.target.value)}
            />
            {submitAttempted && keyErr && <div className={ERR}>{keyErr}</div>}
          </div>
          <div className="flex-1">
            <input
              className={INPUT + " w-full"}
              placeholder={t("dh_edit.schema_field_label")}
              value={label}
              spellCheck={false}
              onChange={(e) => setLabel(e.target.value)}
            />
            {submitAttempted && labelErr && <div className={ERR}>{labelErr}</div>}
          </div>
          <select
            className={INPUT + " w-32"}
            value={type}
            onChange={(e) => setType(e.target.value as InputType)}
          >
            {ALL_TYPES.map((tp) => (
              <option key={tp} value={tp}>{t("dh_edit.type_" + tp)}</option>
            ))}
          </select>
        </div>

        {type === "select" && (
          <div className="flex flex-col gap-1.5 pl-2 border-l-2 border-line">
            <div className="text-[11px] text-faint">{t("dh_edit.schema_options_hint")}</div>
            {options.map((opt, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <input
                  className={INPUT + " flex-1"}
                  placeholder={t("dh_edit.schema_opt_label")}
                  value={opt.label}
                  spellCheck={false}
                  onChange={(e) => {
                    const next = [...options];
                    next[i] = { ...opt, label: e.target.value };
                    setOptions(next);
                  }}
                />
                <input
                  className={INPUT + " flex-1"}
                  placeholder={t("dh_edit.schema_opt_value")}
                  value={opt.value}
                  spellCheck={false}
                  onChange={(e) => {
                    const next = [...options];
                    next[i] = { ...opt, value: e.target.value };
                    setOptions(next);
                  }}
                />
                <button type="button" className={ROW_BTN} onClick={() => setOptions(options.filter((_, idx) => idx !== i))} title={t("dh_edit.list_remove")}>✕</button>
              </div>
            ))}
            <button type="button" className={ROW_BTN + " self-start"} onClick={() => setOptions([...options, { label: "", value: "" }])}>
              + {t("dh_edit.schema_opt_add")}
            </button>
            {submitAttempted && optionsErr && <div className={ERR}>{optionsErr}</div>}
          </div>
        )}

        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-[12px] text-muted cursor-pointer">
            <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} />
            {t("dh_edit.schema_field_required")}
          </label>
          <button type="button" className={ROW_BTN} onClick={submit}>
            + {t("dh_edit.schema_add")}
          </button>
        </div>
      </div>
    </div>
  );
}
