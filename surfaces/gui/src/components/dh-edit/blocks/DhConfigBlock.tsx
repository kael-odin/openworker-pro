// DhConfigBlock — 数字人 config_schema 的动态表单区块（批次 D2）。
// 复用 DigitalHumansSection 的 ConfigFieldInput 样式（boolean→checkbox / select→下拉
// / text→textarea / secret→password）。保存：onChange(config) → PATCH instance.user_config。
import { useT } from "../../../i18n/I18nProvider";
import { GRP, GRP_H } from "../../connectors/ui";
import type { ConfigField } from "../../../api";

const INPUT =
  "px-3 py-1.5 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent w-full";
const LABEL = "text-[12px] font-medium text-muted mb-1 block";

export function DhConfigBlock({
  schema,
  config,
  onChange,
}: {
  schema: ConfigField[];
  config: Record<string, unknown>;
  onChange: (config: Record<string, unknown>) => void;
}) {
  const { t } = useT();
  if (schema.length === 0) return null;

  const setField = (key: string, value: unknown) => {
    onChange({ ...config, [key]: value });
  };

  return (
    <>
      <div className={GRP_H}>{t("dh_edit.blk_config")}</div>
      <div className={GRP + " px-4 py-3"}>
        {schema.map((f) => (
          <ConfigFieldInput
            key={f.key}
            f={f}
            value={config[f.key]}
            onChange={(v) => setField(f.key, v)}
            t={t}
          />
        ))}
      </div>
    </>
  );
}

// 单个 config_schema 字段控件（从 DigitalHumansSection 复刻，保持一致体验）。
function ConfigFieldInput({
  f,
  value,
  onChange,
  t,
}: {
  f: ConfigField;
  value: unknown;
  onChange: (v: unknown) => void;
  t: (k: string) => string;
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
    control = (
      <select
        className={INPUT}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">{t("dh_edit.field_select_ph")}</option>
        {f.options?.map((o) => (
          <option key={String(o.value)} value={String(o.value)}>{o.label}</option>
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
  } else {
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
