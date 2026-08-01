"""DHP ``spec.yaml`` parser — pure, no I/O.

Parses a Digital Human Protocol spec (YAML or JSON) into a :class:`DigitalHumanSpec`. Conforms to
``spec/app-spec.md``: required fields are enforced per app ``type``, legacy field names are
normalized (§15 backward compatibility), subscription shorthand is accepted, and **unknown**
fields are preserved verbatim in ``extra`` rather than rejected — DHP is a versioned protocol and
a future field must not break an older runtime.

The parser is deliberately permissive about *value* shapes it doesn't need (e.g. ``memory_schema``
field types are descriptive, not enforced — per spec §7) but strict about the structural contract
an automation's runtime depends on: a non-empty ``system_prompt``, a resolvable schedule, valid
``config_schema`` keys. A spec that would produce a broken agent fails loudly via :class:`SpecError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# -- enums / constants --------------------------------------------------------

APP_TYPES = ("automation", "skill", "mcp", "extension")
# Input types for config_schema. The first 7 are the original DHP set; the rest extend the form
# editor to cover richer vertical scenarios (collectors, structured config, scheduling, etc.).
#   json        — JSON code editor, syntax-validated at install time
#   stringList  — editable list of strings (keywords, tags); value normalized to string[]
#   urlList     — editable list of URLs (multi-page collectors); value normalized to string[]
#   keyvalue    — editable key/value rows (headers, params); value normalized to [{key,value}]
#   date        — date picker, value "YYYY-MM-DD"
#   datetime    — datetime picker, value ISO 8601
INPUT_TYPES = (
    "string", "text", "number", "boolean", "url", "email", "select",
    "json", "stringList", "urlList", "keyvalue", "date", "datetime",
)
SOURCE_TYPES = ("schedule", "file", "webhook", "webpage", "rss", "custom")
FILTER_OPS = ("eq", "neq", "contains", "matches", "gt", "lt", "gte", "lte")

# A store slug: lowercase alnum + internal hyphens (spec §14).
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
# Duration string: digits + one of s/m/h/d (spec §14).
_DURATION_RE = re.compile(r"^\d+[smhd]$")

# Config-field keys that hold secrets. DHP has no explicit ``secret:`` flag, so a heuristic on the
# key name decides whether a value is routed to SecretStore (never logged / never sent to the model
# in plaintext) vs. stored in the instance JSON. Conservative: only obvious credential names match.
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(token|secret|password|passwd|api[-_]?key|access[-_]?key|credential|cookie|session)(?:$|_)",
    re.IGNORECASE,
)


class SpecError(ValueError):
    """A DHP spec is malformed or violates the structural contract for its app type."""


# -- dataclasses --------------------------------------------------------------


@dataclass
class SelectOption:
    label: str
    value: Any

    def to_dict(self) -> dict:
        return {"label": self.label, "value": self.value}


@dataclass
class ConfigField:
    """One row of ``config_schema`` — the install-time form definition."""

    key: str
    label: str
    type: str  # one of INPUT_TYPES
    description: str = ""
    required: bool = False
    default: Any = None
    placeholder: str = ""
    options: list[SelectOption] = field(default_factory=list)
    # Explicit secret declaration. When True the value is routed to SecretStore regardless of key
    # name. When False the is_secret heuristic on the key name still applies (backward compat).
    secret: bool = False
    # select only: when True the control is multi-select and userConfig stores an array.
    multiple: bool = False
    # number only: range constraints.
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    # Conditional display expression, e.g. 'channel == "webhook"'. The form editor evaluates this
    # against the current userConfig and hides the field when it resolves falsey. See app-spec.md §4.
    visible_if: str = ""

    @property
    def is_secret(self) -> bool:
        """Whether a user value for this field should be stored in SecretStore."""
        return self.secret or bool(_SECRET_KEY_RE.search(self.key))

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
        }
        if self.description:
            d["description"] = self.description
        if self.default is not None:
            d["default"] = self.default
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.options:
            d["options"] = [o.to_dict() for o in self.options]
        if self.secret:
            d["secret"] = True
        if self.multiple:
            d["multiple"] = True
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        if self.step is not None:
            d["step"] = self.step
        if self.visible_if:
            d["visible_if"] = self.visible_if
        d["secret"] = self.is_secret
        return d


@dataclass
class McpDependency:
    id: str
    reason: str = ""
    bundled: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "reason": self.reason, "bundled": self.bundled}


@dataclass
class SkillDependency:
    id: str
    reason: str = ""
    bundled: bool = False
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "reason": self.reason, "bundled": self.bundled, "files": list(self.files)}


@dataclass
class PluginDependency:
    """A plugin the digital human requires (installed from the plugin marketplace)."""

    id: str
    reason: str = ""
    bundled: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "reason": self.reason, "bundled": self.bundled}


@dataclass
class CommandDependency:
    """A slash command the digital human requires (shipped standalone or via a plugin)."""

    id: str
    reason: str = ""
    bundled: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "reason": self.reason, "bundled": self.bundled}


@dataclass
class SubagentDependency:
    """A persona the digital human can delegate subtasks to (via delegate_to_subagent)."""

    id: str
    reason: str = ""
    bundled: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "reason": self.reason, "bundled": self.bundled}


@dataclass
class SubscriptionDef:
    """One trigger source. ``cron`` is the resolved openworker cron expression (from ``every`` or
    ``cron``); ``every`` retains the raw interval for the UI. Non-schedule sources keep ``cron=None``
    (openworker's automation runtime is cron-driven; webhook/file/rss/etc. are recorded for display
    and future runtime support, but only ``schedule`` produces a runnable task today)."""

    id: str
    source_type: str  # one of SOURCE_TYPES
    source_config: dict[str, Any] = field(default_factory=dict)
    cron: Optional[str] = None  # resolved cron for schedule sources
    every: Optional[str] = None  # raw interval string, if schedule+every
    frequency: dict[str, str] = field(default_factory=dict)
    config_key: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "source_config": self.source_config,
            "cron": self.cron,
            "every": self.every,
            "frequency": dict(self.frequency),
            "config_key": self.config_key,
        }


@dataclass
class DigitalHumanSpec:
    """A parsed DHP spec. ``extra`` holds unknown top-level fields verbatim (forward-compat)."""

    name: str
    version: str
    author: str
    description: str
    type: str
    spec_version: str = "1"
    icon: str = ""
    system_prompt: str = ""
    subscriptions: list[SubscriptionDef] = field(default_factory=list)
    config_schema: list[ConfigField] = field(default_factory=list)
    requires_mcps: list[McpDependency] = field(default_factory=list)
    requires_skills: list[SkillDependency] = field(default_factory=list)
    requires_plugins: list[PluginDependency] = field(default_factory=list)
    requires_commands: list[CommandDependency] = field(default_factory=list)
    requires_subagents: list[SubagentDependency] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    memory_schema: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    escalation: dict[str, Any] = field(default_factory=dict)
    mcp_server: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)
    recommended_model: str = ""
    store: dict[str, Any] = field(default_factory=dict)
    i18n: dict[str, Any] = field(default_factory=dict)
    browser_login: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    source_path: Optional[str] = None  # where it was loaded from (provenance)

    @property
    def slug(self) -> str:
        """The registry slug: ``store.slug`` if present and valid, else empty."""
        slug = str(self.store.get("slug") or "").strip()
        return slug if _SLUG_RE.match(slug) else ""

    @property
    def notify_channels(self) -> list[str]:
        """Resolved notify channels from ``output.notify.channels``, normalized to openworker
        channel ids. DHP and openworker share the same channel names."""
        notify = (self.output or {}).get("notify") or {}
        channels = notify.get("channels") or []
        if not isinstance(channels, list):
            return []
        return [str(c).strip() for c in channels if str(c).strip()]

    @property
    def primary_schedule(self) -> Optional[SubscriptionDef]:
        """The first schedule-type subscription (the one that drives the generated task's cron)."""
        for sub in self.subscriptions:
            if sub.source_type == "schedule":
                return sub
        return None

    def to_dict(self) -> dict:
        return {
            "spec_version": self.spec_version,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "type": self.type,
            "icon": self.icon,
            "system_prompt": self.system_prompt,
            "subscriptions": [s.to_dict() for s in self.subscriptions],
            "config_schema": [f.to_dict() for f in self.config_schema],
            "requires": {
                "mcps": [m.to_dict() for m in self.requires_mcps],
                "skills": [s.to_dict() for s in self.requires_skills],
                "plugins": [p.to_dict() for p in self.requires_plugins],
                "commands": [c.to_dict() for c in self.requires_commands],
                "subagents": [s.to_dict() for s in self.requires_subagents],
            },
            "filters": list(self.filters),
            "memory_schema": dict(self.memory_schema),
            "output": dict(self.output),
            "escalation": dict(self.escalation),
            "mcp_server": dict(self.mcp_server),
            "permissions": list(self.permissions),
            "recommended_model": self.recommended_model,
            "store": dict(self.store),
            "slug": self.slug,
            "notify_channels": self.notify_channels,
            "has_schedule": self.primary_schedule is not None,
            "source_path": self.source_path,
        }


# -- parsing ------------------------------------------------------------------


def _require(meta: dict, key: str, slug_hint: str) -> str:
    val = meta.get(key)
    if val is None or str(val).strip() == "":
        raise SpecError(f"spec {slug_hint!r}: missing required field {key!r}")
    return str(val).strip()


def _as_dict_list(val: Any, ctx: str) -> list[dict[str, Any]]:
    if val is None:
        return []
    if not isinstance(val, list):
        raise SpecError(f"{ctx}: expected a list, got {type(val).__name__}")
    out: list[dict[str, Any]] = []
    for item in val:
        if not isinstance(item, dict):
            raise SpecError(f"{ctx}: each item must be a mapping, got {type(item).__name__}")
        out.append(item)
    return out


def _parse_options(raw: Any, ctx: str) -> list[SelectOption]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError(f"{ctx}: `options` must be a list")
    out: list[SelectOption] = []
    for opt in raw:
        if not isinstance(opt, dict):
            raise SpecError(f"{ctx}: each option must be a mapping")
        label = str(opt.get("label", opt.get("value", ""))).strip()
        if not label:
            raise SpecError(f"{ctx}: an option is missing a label")
        out.append(SelectOption(label=label, value=opt.get("value")))
    return out


def _opt_float(val: Any, name: str, ctx: str) -> Optional[float]:
    """Coerce an optional numeric constraint (min/max/step) to float, or None if absent."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        raise SpecError(f"{ctx}: `{name}` must be a number, got {val!r}")


# Case-insensitive lookup for INPUT_TYPES, returning the canonical (exact-case) name. Accepts
# ``stringlist``, ``StringList``, ``STRINGLIST`` etc. and returns ``"stringList"``.
_INPUT_TYPE_LUT = {t.lower(): t for t in INPUT_TYPES}


def _canonical_input_type(raw: str) -> Optional[str]:
    if not raw:
        return None
    return _INPUT_TYPE_LUT.get(raw.strip().lower())


def _parse_config_schema(raw: Any, slug_hint: str) -> list[ConfigField]:
    fields: list[ConfigField] = []
    for i, item in enumerate(_as_dict_list(raw, f"spec {slug_hint!r}: config_schema")):
        ctx = f"spec {slug_hint!r}: config_schema[{i}]"
        key = str(item.get("key") or "").strip()
        if not key:
            raise SpecError(f"{ctx}: missing `key`")
        label = str(item.get("label") or "").strip()
        if not label:
            raise SpecError(f"{ctx}: missing `label`")
        ftype_raw = str(item.get("type") or "").strip()
        ftype = _canonical_input_type(ftype_raw)
        if ftype is None:
            raise SpecError(
                f"{ctx}: type {ftype_raw!r} is not one of {list(INPUT_TYPES)}"
            )
        if ftype == "select" and not item.get("options"):
            raise SpecError(f"{ctx}: type `select` requires `options`")
        # number range constraints — coerce to float if present.
        min_v = _opt_float(item.get("min"), "min", ctx)
        max_v = _opt_float(item.get("max"), "max", ctx)
        step_v = _opt_float(item.get("step"), "step", ctx)
        if ftype != "number" and (min_v is not None or max_v is not None or step_v is not None):
            raise SpecError(f"{ctx}: min/max/step are only valid for type `number`")
        if ftype != "select" and item.get("multiple"):
            raise SpecError(f"{ctx}: `multiple` is only valid for type `select`")
        fields.append(
            ConfigField(
                key=key,
                label=label,
                type=ftype,
                description=str(item.get("description", "")).strip(),
                required=bool(item.get("required", False)),
                default=item.get("default"),
                placeholder=str(item.get("placeholder", "")).strip(),
                options=_parse_options(item.get("options"), ctx),
                secret=bool(item.get("secret", False)),
                multiple=bool(item.get("multiple", False)),
                min=min_v,
                max=max_v,
                step=step_v,
                visible_if=str(item.get("visible_if", "") or "").strip(),
            )
        )
    # Duplicate keys confuse userConfig injection — reject.
    seen = set()
    for f in fields:
        if f.key in seen:
            raise SpecError(f"spec {slug_hint!r}: duplicate config_schema key {f.key!r}")
        seen.add(f.key)
    return fields


def apply_schema_override(spec: "DigitalHumanSpec", override_raw: Any, slug_hint: str) -> None:
    """Replace ``spec.config_schema`` with a parsed override, in place.

    The manager re-fetches the spec from the remote registry on every update, so schema edits made
    in the form editor cannot live on the spec itself — they are stored on the instance as a raw
    list-of-dicts override and re-applied here whenever the spec is loaded. ``override_raw`` is the
    same shape as the ``config_schema`` YAML node. An empty/None override is a no-op. A malformed
    override raises :class:`SpecError` so the caller surfaces invalid edits.
    """
    if not override_raw:
        return
    spec.config_schema = _parse_config_schema(override_raw, slug_hint)


def _every_to_cron(every: str, slug_hint: str) -> str:
    """Convert a DHP duration string (``30m``, ``2h``, ``1d``) to a 5-field cron expression.

    Days → ``0 0 */N * *`` (daily at midnight every N days). Hours/minutes → ``*/N`` in the
    matching field. Seconds are clamped to every minute (cron has no sub-minute resolution)."""
    if not _DURATION_RE.match(every):
        raise SpecError(f"spec {slug_hint!r}: invalid duration {every!r} (expected like '30m', '2h', '1d')")
    n = int(every[:-1])
    unit = every[-1]
    if n <= 0:
        raise SpecError(f"spec {slug_hint!r}: duration must be positive, got {every!r}")
    if unit == "s":
        return "* * * * *"  # sub-minute → every minute
    if unit == "m":
        # cron `*/N` only divides evenly into 60 for N in {1,2,3,4,5,6,10,12,15,20,30}; for other N
        # (or N>59) fall back to every minute. A non-even divisor would fire at irregular offsets.
        return f"*/{n} * * * *" if n <= 59 and 60 % n == 0 else "* * * * *"
    if unit == "h":
        # `*/N` divides evenly into 24 for N in {1,2,3,4,6,8,12}; for N≥24 (e.g. "24h" = daily) or
        # non-divisors, fall back to daily at midnight — the closest stable cadence.
        if n <= 23 and 24 % n == 0:
            return f"0 */{n} * * *"
        return "0 0 * * *"  # daily at midnight
    if unit == "d":
        return f"0 0 */{n} * *"
    raise SpecError(f"spec {slug_hint!r}: unhandled duration unit {unit!r}")  # pragma: no cover


def _parse_subscriptions(raw: Any, slug_hint: str) -> list[SubscriptionDef]:
    out: list[SubscriptionDef] = []
    for i, item in enumerate(_as_dict_list(raw, f"spec {slug_hint!r}: subscriptions")):
        ctx = f"spec {slug_hint!r}: subscriptions[{i}]"
        # §15 shorthand: `type`/`config` at entry level == nested under `source`.
        source = item.get("source")
        if isinstance(source, dict):
            stype = str(source.get("type") or "").strip().lower()
            sconfig = source.get("config") or {}
        else:
            stype = str(item.get("type") or "").strip().lower()
            sconfig = item.get("config") or {}
        if stype not in SOURCE_TYPES:
            raise SpecError(
                f"{ctx}: source type {stype!r} is not one of {list(SOURCE_TYPES)}"
            )
        if not isinstance(sconfig, dict):
            raise SpecError(f"{ctx}: source config must be a mapping")

        cron: Optional[str] = None
        every: Optional[str] = None
        if stype == "schedule":
            c_every = str(sconfig.get("every") or "").strip()
            c_cron = str(sconfig.get("cron") or "").strip()
            if c_every and c_cron:
                raise SpecError(f"{ctx}: `every` and `cron` are mutually exclusive")
            if not c_every and not c_cron:
                raise SpecError(f"{ctx}: schedule needs `every` or `cron`")
            if c_cron:
                cron = c_cron
            else:
                every = c_every
                cron = _every_to_cron(c_every, slug_hint)

        freq_raw = item.get("frequency") or {}
        frequency = (
            {k: str(v) for k, v in freq_raw.items()} if isinstance(freq_raw, dict) else {}
        )

        out.append(
            SubscriptionDef(
                id=str(item.get("id") or f"sub-{i}"),
                source_type=stype,
                source_config=dict(sconfig),
                cron=cron,
                every=every,
                frequency=frequency,
                config_key=str(item.get("config_key") or item.get("input") or "").strip(),
            )
        )
    return out


def _parse_requires(
    raw: Any, slug_hint: str
) -> tuple[
    list[McpDependency],
    list[SkillDependency],
    list[PluginDependency],
    list[CommandDependency],
    list[SubagentDependency],
]:
    if raw is None:
        return [], [], [], [], []
    if not isinstance(raw, dict):
        raise SpecError(f"spec {slug_hint!r}: `requires` must be a mapping")

    # §15 legacy aliases: top-level `required_mcps`/`required_skills`, and singular `mcp`/`skill`
    # inside `requires`. All normalized to the canonical plural object form.
    mcps_raw = raw.get("mcps")
    if mcps_raw is None:
        mcps_raw = raw.get("mcp")
    skills_raw = raw.get("skills")
    if skills_raw is None:
        skills_raw = raw.get("skill")

    mcps: list[McpDependency] = []
    for item in _normalize_dep_list(mcps_raw, f"spec {slug_hint!r}: requires.mcps"):
        mcps.append(
            McpDependency(
                id=str(item["id"]).strip(),
                reason=str(item.get("reason", "")).strip(),
                bundled=bool(item.get("bundled", False)),
            )
        )

    skills: list[SkillDependency] = []
    for item in _normalize_dep_list(skills_raw, f"spec {slug_hint!r}: requires.skills"):
        files = item.get("files") or []
        skills.append(
            SkillDependency(
                id=str(item["id"]).strip(),
                reason=str(item.get("reason", "")).strip(),
                bundled=bool(item.get("bundled", False)),
                files=[str(f) for f in files] if isinstance(files, list) else [],
            )
        )

    plugins: list[PluginDependency] = []
    for item in _normalize_dep_list(raw.get("plugins"), f"spec {slug_hint!r}: requires.plugins"):
        plugins.append(
            PluginDependency(
                id=str(item["id"]).strip(),
                reason=str(item.get("reason", "")).strip(),
                bundled=bool(item.get("bundled", False)),
            )
        )

    commands: list[CommandDependency] = []
    for item in _normalize_dep_list(raw.get("commands"), f"spec {slug_hint!r}: requires.commands"):
        commands.append(
            CommandDependency(
                id=str(item["id"]).strip(),
                reason=str(item.get("reason", "")).strip(),
                bundled=bool(item.get("bundled", False)),
            )
        )

    subagents: list[SubagentDependency] = []
    for item in _normalize_dep_list(raw.get("subagents"), f"spec {slug_hint!r}: requires.subagents"):
        subagents.append(
            SubagentDependency(
                id=str(item["id"]).strip(),
                reason=str(item.get("reason", "")).strip(),
                bundled=bool(item.get("bundled", False)),
            )
        )

    return mcps, skills, plugins, commands, subagents


def _normalize_dep_list(raw: Any, ctx: str) -> list[dict[str, Any]]:
    """Accept shorthand (string ids) or object form, per spec §5."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError(f"{ctx}: expected a list")
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            out.append({"id": item})
        elif isinstance(item, dict):
            if not item.get("id"):
                raise SpecError(f"{ctx}: an entry is missing `id`")
            out.append(item)
        else:
            raise SpecError(f"{ctx}: entries must be strings or mappings")
    return out


# Known top-level keys — anything else goes into `extra` (forward-compat).
_KNOWN_KEYS = {
    "spec_version", "name", "version", "author", "description", "type", "icon",
    "system_prompt", "subscriptions", "config_schema", "requires", "filters",
    "memory_schema", "output", "escalation", "mcp_server", "permissions",
    "recommended_model", "store", "i18n", "browser_login",
    # Legacy aliases (consumed during normalization, not stored under the canonical name).
    "inputs", "required_mcps", "required_skills",
}


def parse_spec(text: str, *, source: Optional[str] = None) -> DigitalHumanSpec:
    """Parse a DHP spec (YAML or JSON text) into a :class:`DigitalHumanSpec`.

    Raises :class:`SpecError` on a structural violation. Unknown fields are preserved in
    ``spec.extra`` so a newer protocol version degrades gracefully.
    """
    try:
        meta = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise SpecError(f"invalid YAML/JSON: {e}") from e
    if not isinstance(meta, dict):
        raise SpecError("spec must be a mapping at the top level")

    slug_hint = source or str(meta.get("name") or meta.get("store", {}).get("slug") or "spec")

    name = _require(meta, "name", slug_hint)
    version = _require(meta, "version", slug_hint)
    author = _require(meta, "author", slug_hint)
    description = _require(meta, "description", slug_hint)
    atype = str(meta.get("type") or "").strip().lower()
    if atype not in APP_TYPES:
        raise SpecError(
            f"spec {slug_hint!r}: type {atype!r} is not one of {list(APP_TYPES)}"
        )

    spec_version = str(meta.get("spec_version") or "1")
    icon = str(meta.get("icon") or "").strip()
    system_prompt = str(meta.get("system_prompt") or "").strip()

    # §15 legacy aliases: `inputs` → `config_schema`; `required_mcps/skills` → `requires`.
    config_raw = meta.get("config_schema")
    if config_raw is None:
        config_raw = meta.get("inputs")

    requires_raw = meta.get("requires")
    if requires_raw is None and (meta.get("required_mcps") is not None or meta.get("required_skills") is not None):
        requires_raw = {
            "mcps": meta.get("required_mcps"),
            "skills": meta.get("required_skills"),
        }

    # Type-specific enforcement (spec §1).
    if atype in ("automation", "skill") and not system_prompt:
        raise SpecError(f"spec {slug_hint!r}: type {atype!r} requires a non-empty `system_prompt`")
    if atype == "mcp" and not meta.get("mcp_server"):
        raise SpecError(f"spec {slug_hint!r}: type `mcp` requires `mcp_server`")

    subscriptions = _parse_subscriptions(meta.get("subscriptions"), slug_hint)
    # Spec §1 says automation requires ≥1 subscription, but real published specs use `subscriptions:
    # []` for manually-triggered automations (skill-like). Treat an empty list as a valid
    # manual-run automation (``has_schedule=False``) rather than rejecting — match the runtime.
    if atype != "automation" and subscriptions:
        raise SpecError(f"spec {slug_hint!r}: only `automation` may declare `subscriptions`")

    config_schema = _parse_config_schema(config_raw, slug_hint)
    requires_mcps, requires_skills, requires_plugins, requires_commands, requires_subagents = _parse_requires(requires_raw, slug_hint)

    extra = {k: v for k, v in meta.items() if k not in _KNOWN_KEYS}

    return DigitalHumanSpec(
        name=name,
        version=version,
        author=author,
        description=description,
        type=atype,
        spec_version=spec_version,
        icon=icon,
        system_prompt=system_prompt,
        subscriptions=subscriptions,
        config_schema=config_schema,
        requires_mcps=requires_mcps,
        requires_skills=requires_skills,
        requires_plugins=requires_plugins,
        requires_commands=requires_commands,
        requires_subagents=requires_subagents,
        filters=_as_dict_list(meta.get("filters"), f"spec {slug_hint!r}: filters"),
        memory_schema=meta.get("memory_schema") if isinstance(meta.get("memory_schema"), dict) else {},
        output=meta.get("output") if isinstance(meta.get("output"), dict) else {},
        escalation=meta.get("escalation") if isinstance(meta.get("escalation"), dict) else {},
        mcp_server=meta.get("mcp_server") if isinstance(meta.get("mcp_server"), dict) else {},
        permissions=[str(p) for p in (meta.get("permissions") or []) if isinstance(p, str)],
        recommended_model=str(meta.get("recommended_model") or "").strip(),
        store=meta.get("store") if isinstance(meta.get("store"), dict) else {},
        i18n=meta.get("i18n") if isinstance(meta.get("i18n"), dict) else {},
        browser_login=_as_dict_list(meta.get("browser_login"), f"spec {slug_hint!r}: browser_login"),
        extra=extra,
        source_path=source,
    )


def load_spec_file(path: str | Path) -> DigitalHumanSpec:
    p = Path(path)
    return parse_spec(p.read_text(encoding="utf-8"), source=str(p))
