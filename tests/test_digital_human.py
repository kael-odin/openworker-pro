"""Tests for the DHP digital-human bridge (批次 B): spec parser, registry, instances, installer.

Covers the full path: parsing → registry resolution → install → ScheduledTask generation →
instance record → secret routing. Parser edge cases (legacy aliases, shorthand, unknown fields,
type-specific enforcement) are exercised against synthetic specs; registry + installer are also
validated against the real DHP repo clone when present (skip otherwise).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from coworker.digital_human.spec import (
    DigitalHumanSpec,
    SpecError,
    parse_spec,
    _every_to_cron,
)
from coworker.digital_human.store import DhpRegistry
from coworker.digital_human.instances import InstanceStore, DigitalHumanInstance
from coworker.digital_human.installer import (
    install_digital_human,
    uninstall_digital_human,
    build_instructions,
    validate_config,
    split_config,
)

_dhp_env = os.environ.get("OPENWORKER_DHP_REPO", "").strip()
DHP_REPO = (
    Path(_dhp_env).resolve()
    if _dhp_env
    else Path(__file__).resolve().parents[2] / "digital-human-protocol"
)
# Path("").resolve() returns the cwd (truthy), so guard on the env string, not
# the resolved Path — otherwise skipif never triggers when the repo is absent.


# -- spec parser: minimal + required-field enforcement ------------------------


MINIMAL = """
spec_version: "1"
name: Test Agent
version: "1.0.0"
author: tester
description: A test agent.
type: automation
system_prompt: |
  You are a test agent. Do the thing.
subscriptions:
  - source:
      type: schedule
      config:
        every: 1h
"""


def test_parse_minimal_automation():
    s = parse_spec(MINIMAL)
    assert s.name == "Test Agent"
    assert s.type == "automation"
    assert s.system_prompt.strip().startswith("You are a test agent")
    assert s.primary_schedule is not None
    assert s.primary_schedule.cron == "0 */1 * * *"
    assert s.primary_schedule.every == "1h"
    assert s.notify_channels == []


def test_missing_required_field():
    with pytest.raises(SpecError, match="missing required field 'name'"):
        parse_spec("version: '1'\ntype: automation\nsystem_prompt: x\nsubscriptions: []")


def test_missing_system_prompt_for_automation():
    bad = "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: automation\nsubscriptions: []\n"
    with pytest.raises(SpecError, match="requires a non-empty `system_prompt`"):
        parse_spec(bad)


def test_mcp_type_requires_mcp_server():
    bad = "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: mcp\n"
    with pytest.raises(SpecError, match="type `mcp` requires `mcp_server`"):
        parse_spec(bad)


def test_non_automation_with_subscriptions_rejected():
    bad = (
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 1h\n"
    )
    with pytest.raises(SpecError, match="only `automation` may declare"):
        parse_spec(bad)


def test_empty_subscriptions_allowed_for_manual_run():
    """`subscriptions: []` is a real published pattern (manual-run automation) — accepted."""
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: automation\n"
        "system_prompt: y\nsubscriptions: []\n"
    )
    assert s.subscriptions == []
    assert s.primary_schedule is None


def test_invalid_app_type():
    bad = "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: robot\nsystem_prompt: y\n"
    with pytest.raises(SpecError, match="type 'robot' is not one of"):
        parse_spec(bad)


# -- every → cron conversion --------------------------------------------------


@pytest.mark.parametrize(
    "every,expected",
    [
        ("30m", "*/30 * * * *"),
        ("10m", "*/10 * * * *"),
        ("1h", "0 */1 * * *"),
        ("2h", "0 */2 * * *"),
        ("6h", "0 */6 * * *"),
        ("12h", "0 */12 * * *"),
        ("24h", "0 0 * * *"),  # non-divisor of 24 → daily at midnight
        ("1d", "0 0 */1 * *"),
        ("45s", "* * * * *"),  # sub-minute → every minute
        ("7m", "* * * * *"),  # 60 % 7 != 0 → every minute (irregular divisor)
    ],
)
def test_every_to_cron(every, expected):
    assert _every_to_cron(every, "test") == expected


def test_every_and_cron_mutually_exclusive():
    bad = (
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n"
        "        every: 1h\n        cron: '0 0 * * *'\n"
    )
    with pytest.raises(SpecError, match="mutually exclusive"):
        parse_spec(bad)


def test_schedule_needs_every_or_cron():
    bad = (
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config: {}\n"
    )
    with pytest.raises(SpecError, match="needs `every` or `cron`"):
        parse_spec(bad)


def test_raw_cron_preserved():
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n"
        "        cron: '30 15 * * 1-5'\n"
    )
    assert s.primary_schedule.cron == "30 15 * * 1-5"
    assert s.primary_schedule.every is None


# -- config_schema ------------------------------------------------------------


def test_config_schema_parsed_with_options():
    spec = (
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "config_schema:\n"
        "  - key: lang\n    label: Language\n    type: select\n    required: true\n"
        "    options:\n      - label: Chinese\n        value: zh\n      - label: English\n        value: en\n"
        "  - key: count\n    label: Count\n    type: number\n    default: 5\n"
    )
    s = parse_spec(spec)
    assert len(s.config_schema) == 2
    assert s.config_schema[0].type == "select"
    assert len(s.config_schema[0].options) == 2
    assert s.config_schema[0].options[0].value == "zh"
    assert s.config_schema[1].default == 5


def test_select_requires_options():
    bad = (
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "config_schema:\n  - key: lang\n    label: L\n    type: select\n"
    )
    with pytest.raises(SpecError, match="type `select` requires `options`"):
        parse_spec(bad)


def test_duplicate_config_keys_rejected():
    bad = (
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "config_schema:\n  - key: a\n    label: A\n    type: string\n"
        "  - key: a\n    label: A2\n    type: string\n"
    )
    with pytest.raises(SpecError, match="duplicate config_schema key 'a'"):
        parse_spec(bad)


def test_secret_field_heuristic():
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "config_schema:\n"
        "  - key: api_key\n    label: API Key\n    type: string\n"
        "  - key: webhook_url\n    label: URL\n    type: url\n"
        "  - key: cookie_token\n    label: Cookie\n    type: string\n"
        "  - key: normal_field\n    label: N\n    type: string\n"
    )
    keys = {f.key: f.is_secret for f in s.config_schema}
    assert keys["api_key"] is True
    assert keys["cookie_token"] is True
    assert keys["webhook_url"] is False
    assert keys["normal_field"] is False


# -- extended input types + override (13-type system) ------------------------


_BASE_SPEC = (
    "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
)


def test_all_13_input_types_parse():
    """parse_spec accepts every InputType, and new ConfigField attrs round-trip."""
    from coworker.digital_human.spec import INPUT_TYPES
    assert len(INPUT_TYPES) == 13
    s = parse_spec(
        _BASE_SPEC + "config_schema:\n"
        "  - {key: s, label: S, type: string}\n"
        "  - {key: t, label: T, type: text}\n"
        "  - {key: n, label: N, type: number, min: 0, max: 100, step: 5, default: 50}\n"
        "  - {key: b, label: B, type: boolean, default: false}\n"
        "  - {key: u, label: U, type: url}\n"
        "  - {key: e, label: E, type: email}\n"
        "  - {key: sl, label: SL, type: select, options: [{label: A, value: a}]}\n"
        "  - {key: js, label: JS, type: json, default: '{}'}\n"
        "  - {key: strlist, label: SL2, type: stringList}\n"
        "  - {key: urllist, label: UL, type: urlList}\n"
        "  - {key: kv, label: KV, type: keyvalue}\n"
        "  - {key: d, label: D, type: date}\n"
        "  - {key: dt, label: DT, type: datetime}\n"
    )
    assert len(s.config_schema) == 13
    types = [f.type for f in s.config_schema]
    assert types == list(INPUT_TYPES)
    # number constraints
    n = s.config_schema[2]
    assert n.min == 0.0 and n.max == 100.0 and n.step == 5.0 and n.default == 50
    # to_dict serializes the new fields
    d = n.to_dict()
    assert d["min"] == 0.0 and d["max"] == 100.0 and d["step"] == 5.0


def test_type_match_is_case_insensitive():
    """stringList / stringlist / STRINGLIST all resolve to the canonical name."""
    s = parse_spec(
        _BASE_SPEC + "config_schema:\n"
        "  - {key: a, label: A, type: stringlist}\n"
        "  - {key: b, label: B, type: URLList}\n"
    )
    assert s.config_schema[0].type == "stringList"
    assert s.config_schema[1].type == "urlList"


def test_number_constraints_only_valid_on_number():
    with pytest.raises(SpecError, match="min/max/step are only valid for type `number`"):
        parse_spec(_BASE_SPEC + "config_schema:\n  - {key: s, label: S, type: string, min: 0}\n")


def test_multiple_only_valid_on_select():
    with pytest.raises(SpecError, match="`multiple` is only valid for type `select`"):
        parse_spec(_BASE_SPEC + "config_schema:\n  - {key: s, label: S, type: string, multiple: true}\n")


def test_explicit_secret_flag_overrides_heuristic():
    """A field with secret: true is secret regardless of key name."""
    from coworker.digital_human.spec import _SECRET_KEY_RE
    s = parse_spec(
        _BASE_SPEC + "config_schema:\n"
        "  - {key: normal_name, label: N, type: string, secret: true}\n"
    )
    f = s.config_schema[0]
    # heuristic alone would say False (no credential-ish keyword in "normal_name")
    assert not _SECRET_KEY_RE.search("normal_name")
    assert f.is_secret is True  # explicit flag wins
    assert f.to_dict()["secret"] is True


def test_validate_config_json_parses():
    s = parse_spec(_BASE_SPEC + "config_schema:\n  - {key: js, label: JS, type: json}\n")
    cfg = {"js": '{"k": 1}'}
    errs = validate_config(s, cfg)
    assert errs == []
    assert cfg["js"] == {"k": 1}


def test_validate_config_json_invalid_reports_error():
    s = parse_spec(_BASE_SPEC + "config_schema:\n  - {key: js, label: JS, type: json}\n")
    cfg = {"js": "{not valid"}
    errs = validate_config(s, cfg)
    assert "js" in errs


def test_validate_config_stringlist_normalizes_string_to_array():
    s = parse_spec(_BASE_SPEC + "config_schema:\n  - {key: tags, label: T, type: stringList}\n")
    cfg = {"tags": "alpha, beta\ngamma"}
    errs = validate_config(s, cfg)
    assert errs == []
    assert cfg["tags"] == ["alpha", "beta", "gamma"]


def test_validate_config_urllist_rejects_non_http():
    s = parse_spec(_BASE_SPEC + "config_schema:\n  - {key: urls, label: U, type: urlList}\n")
    cfg = {"urls": ["https://ok.com", "ftp://bad"]}
    errs = validate_config(s, cfg)
    assert "urls" in errs


def test_validate_config_keyvalue_normalizes_object():
    s = parse_spec(_BASE_SPEC + "config_schema:\n  - {key: h, label: H, type: keyvalue}\n")
    cfg = {"h": {"X-Key": "val", "Y": "2"}}
    errs = validate_config(s, cfg)
    assert errs == []
    assert cfg["h"] == [{"key": "X-Key", "value": "val"}, {"key": "Y", "value": "2"}]


def test_validate_config_date_and_datetime():
    s = parse_spec(
        _BASE_SPEC + "config_schema:\n"
        "  - {key: d, label: D, type: date}\n"
        "  - {key: dt, label: DT, type: datetime}\n"
    )
    # valid
    cfg = {"d": "2026-08-01", "dt": "2026-08-01T10:00:00Z"}
    assert validate_config(s, cfg) == []
    # invalid date
    cfg2 = {"d": "08/01/2026", "dt": "2026-08-01T10:00:00Z"}
    assert "d" in validate_config(s, cfg2)
    # invalid datetime
    cfg3 = {"d": "2026-08-01", "dt": "yesterday"}
    assert "dt" in validate_config(s, cfg3)


def test_validate_config_number_min_max():
    s = parse_spec(
        _BASE_SPEC + "config_schema:\n  - {key: n, label: N, type: number, min: 1, max: 10}\n"
    )
    assert validate_config(s, {"n": 5}) == []
    assert "n" in validate_config(s, {"n": 0})   # below min
    assert "n" in validate_config(s, {"n": 11})  # above max


def test_validate_config_select_multiple_validates_each():
    s = parse_spec(
        _BASE_SPEC + "config_schema:\n"
        "  - {key: sev, label: S, type: select, multiple: true,\n"
        "     options: [{label: H, value: high}, {label: L, value: low}]}\n"
    )
    assert validate_config(s, {"sev": "high, low"}) == []  # string normalizes + validates
    assert "sev" in validate_config(s, {"sev": "high, bogus"})  # bogus not in options


def test_apply_schema_override_replaces_schema():
    from coworker.digital_human.spec import apply_schema_override
    s = parse_spec(_BASE_SPEC + "config_schema:\n  - {key: a, label: A, type: string}\n")
    assert len(s.config_schema) == 1
    apply_schema_override(s, [{"key": "b", "label": "B", "type": "json"}], "x")
    assert len(s.config_schema) == 1
    assert s.config_schema[0].key == "b" and s.config_schema[0].type == "json"


def test_apply_schema_override_empty_is_noop():
    from coworker.digital_human.spec import apply_schema_override
    s = parse_spec(_BASE_SPEC + "config_schema:\n  - {key: a, label: A, type: string}\n")
    before = list(s.config_schema)
    apply_schema_override(s, [], "x")
    assert s.config_schema == before
    apply_schema_override(s, None, "x")
    assert s.config_schema == before


def test_apply_schema_override_rejects_bad_shape():
    from coworker.digital_human.spec import apply_schema_override
    s = parse_spec(_BASE_SPEC + "config_schema:\n  - {key: a, label: A, type: string}\n")
    with pytest.raises(SpecError, match="duplicate config_schema key"):
        apply_schema_override(s, [
            {"key": "dup", "label": "D", "type": "string"},
            {"key": "dup", "label": "D2", "type": "string"},
        ], "x")


def test_instance_config_schema_override_round_trip():
    inst = DigitalHumanInstance(id="dh-x", slug="s", name="n", task_id="t")
    inst.config_schema_override = [{"key": "o", "label": "O", "type": "json"}]
    d = inst.to_dict()
    assert d["config_schema_override"] == [{"key": "o", "label": "O", "type": "json"}]
    inst2 = DigitalHumanInstance.from_dict(d)
    assert inst2.config_schema_override == inst.config_schema_override


def test_legacy_instance_has_empty_override():
    d = {"id": "x", "slug": "s", "name": "n", "task_id": "t"}
    inst = DigitalHumanInstance.from_dict(d)
    assert inst.config_schema_override == []


# -- legacy aliases + shorthand + unknown fields ------------------------------


def test_legacy_inputs_alias():
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "inputs:\n  - key: a\n    label: A\n    type: string\n"
    )
    assert len(s.config_schema) == 1
    assert s.config_schema[0].key == "a"


def test_legacy_required_mcps_alias():
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "required_mcps:\n  - ai-browser\n"
    )
    assert len(s.requires_mcps) == 1
    assert s.requires_mcps[0].id == "ai-browser"


def test_requires_plugins_commands_subagents_parsed():
    """E4: spec.requires unifies {mcps, skills, plugins, commands, subagents}."""
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "requires:\n"
        "  plugins:\n"
        "    - id: chrome-devtools\n      reason: inspect browser\n"
        "  commands:\n"
        "    - review\n"
        "  subagents:\n"
        "    - id: researcher\n"
    )
    assert len(s.requires_plugins) == 1
    assert s.requires_plugins[0].id == "chrome-devtools"
    assert s.requires_plugins[0].reason == "inspect browser"
    assert len(s.requires_commands) == 1
    assert s.requires_commands[0].id == "review"
    assert len(s.requires_subagents) == 1
    assert s.requires_subagents[0].id == "researcher"
    # to_dict surfaces all seven requires kinds (rules/hooks are soft deps, included
    # even when the spec declares none — empty lists, same as the other kinds).
    d = s.to_dict()
    assert set(d["requires"].keys()) == {
        "mcps", "skills", "plugins", "commands", "subagents", "rules", "hooks"
    }
    assert d["requires"]["rules"] == []
    assert d["requires"]["hooks"] == []


def test_subscription_shorthand():
    """§15: type/config at entry level == nested under source."""
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y\n"
        "subscriptions:\n  - type: schedule\n    config:\n      every: 2h\n"
    )
    assert s.primary_schedule.cron == "0 */2 * * *"


def test_unknown_fields_preserved_in_extra():
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "future_field: some-value\nanother_new:\n  nested: true\n"
    )
    assert s.extra["future_field"] == "some-value"
    assert s.extra["another_new"]["nested"] is True


def test_notify_channels_from_output():
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 1h\n"
        "output:\n  notify:\n    system: true\n    channels:\n      - dingtalk\n      - email\n"
    )
    assert s.notify_channels == ["dingtalk", "email"]


def test_store_slug_extraction():
    s = parse_spec(
        "name: x\nversion: '1'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: y\n"
        "store:\n  slug: my-cool-agent\n  category: dev-tools\n"
    )
    assert s.slug == "my-cool-agent"


# -- installer: config validation + preamble ----------------------------------


def _skill_spec(config_schema=""):
    cs = config_schema or ""
    return parse_spec(
        "name: Tester\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: skill\nsystem_prompt: Do the thing.\n"
        + cs
    )


def test_validate_config_fills_defaults():
    s = _skill_spec(
        "config_schema:\n  - key: n\n    label: N\n    type: number\n    default: 7\n"
    )
    cfg = {}
    missing = validate_config(s, cfg)
    assert missing == []
    assert cfg["n"] == 7


def test_validate_config_reports_missing_required():
    s = _skill_spec(
        "config_schema:\n  - key: url\n    label: U\n    type: url\n    required: true\n"
    )
    missing = validate_config(s, {})
    assert missing == ["url"]


def test_validate_config_coerces_types():
    s = _skill_spec(
        "config_schema:\n  - key: n\n    label: N\n    type: number\n    default: 1\n"
        "  - key: flag\n    label: F\n    type: boolean\n    default: false\n"
    )
    cfg = {"n": "10", "flag": "true"}
    validate_config(s, cfg)
    assert cfg["n"] == 10
    assert cfg["flag"] is True


def test_build_includes_config_preamble():
    s = _skill_spec()
    out = build_instructions(s, {"topic": "AI", "count": 5})
    assert "## 用户配置 (userConfig)" in out
    assert "Do the thing." in out
    assert "topic" in out and "AI" in out


def test_split_config_separates_secrets():
    s = _skill_spec(
        "config_schema:\n  - key: api_key\n    label: K\n    type: string\n"
        "  - key: topic\n    label: T\n    type: string\n"
    )
    non_secret, secret, keys = split_config(s, {"api_key": "sk-123", "topic": "news"})
    assert non_secret == {"topic": "news"}
    assert secret == {"api_key": "sk-123"}
    assert keys == ["api_key"]


# -- installer: full install → ScheduledTask + instance -----------------------


class _FakeTaskStore:
    """Minimal TaskStore stand-in: save/get/delete in memory."""

    def __init__(self):
        self._tasks = {}

    def save(self, task):
        self._tasks[task.id] = task

    def get(self, task_id):
        return self._tasks.get(task_id)

    def delete(self, task_id):
        return self._tasks.pop(task_id, None) is not None


def _make_instances(tmp_path):
    return InstanceStore(tmp_path / "dh.json")


def test_install_creates_task_and_instance(tmp_path):
    s = parse_spec(
        "name: News Bot\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: Report news.\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 24h\n"
        "config_schema:\n  - key: topic\n    label: Topic\n    type: string\n    default: AI\n"
        "output:\n  notify:\n    channels:\n      - dingtalk\n"
    )
    ts = _FakeTaskStore()
    insts = _make_instances(tmp_path)
    result = install_digital_human(
        s, {}, task_store=ts, scratch_provider=lambda sid: f"/tmp/{sid}", instances=insts
    )
    assert result["ok"], result
    inst = result["instance"]
    task = result["task"]
    assert inst["slug"] == s.slug or inst["name"] == "News Bot"
    assert task["schedule_raw"]["cron"] == "0 0 * * *"  # 24h → daily
    assert task["notify_channels"] == ["dingtalk"]
    assert task["notify_level"] == "all"  # spec declared channels
    assert task["enabled"] is True
    # Instance persisted + task linked.
    assert insts.get(inst["id"]) is not None
    assert ts.get(inst["task_id"]) is not None
    # Instructions carry the config preamble.
    assert "## 用户配置" in ts.get(inst["task_id"]).instructions


def test_install_manual_run_spec_is_disabled(tmp_path):
    s = parse_spec(
        "name: Manual\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\n"
        "system_prompt: Run on demand.\nsubscriptions: []\n"
    )
    ts = _FakeTaskStore()
    insts = _make_instances(tmp_path)
    result = install_digital_human(
        s, {}, task_store=ts, scratch_provider=lambda sid: f"/tmp/{sid}", instances=insts
    )
    assert result["ok"]
    assert result["task"]["enabled"] is False
    assert result["task"]["schedule_raw"]["cron"] == "0 0 30 2 *"  # never-fires


def test_install_missing_required_config_fails(tmp_path):
    s = parse_spec(
        "name: X\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y.\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 1h\n"
        "config_schema:\n  - key: url\n    label: U\n    type: url\n    required: true\n"
    )
    result = install_digital_human(
        s, {}, task_store=_FakeTaskStore(),
        scratch_provider=lambda sid: sid, instances=_make_instances(tmp_path),
    )
    assert not result["ok"]
    assert "url" in result["error"]


def test_install_routes_secret_to_store(tmp_path):
    from coworker.secrets import SecretStore

    s = parse_spec(
        "name: X\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y.\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 1h\n"
        "config_schema:\n  - key: api_token\n    label: T\n    type: string\n    required: true\n"
        "  - key: topic\n    label: Topic\n    type: string\n    default: news\n"
    )
    ts = _FakeTaskStore()
    secrets = SecretStore(tmp_path / "secrets.json")
    insts = InstanceStore(tmp_path / "dh.json", secrets=secrets)
    result = install_digital_human(
        s, {"api_token": "sk-secret"},
        task_store=ts, scratch_provider=lambda sid: sid, instances=insts,
    )
    assert result["ok"]
    inst = insts.get(result["instance"]["id"])
    assert inst.secret_keys == ["api_token"]
    assert "api_token" not in inst.config  # not in plaintext instance record
    # Secret is in the SecretStore under the instance profile.
    secret_data = secrets.get(inst.secret_profile())
    assert secret_data == {"api_token": "sk-secret"}


def test_uninstall_removes_task_and_instance(tmp_path):
    s = parse_spec(
        "name: X\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y.\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 1h\n"
    )
    ts = _FakeTaskStore()
    insts = _make_instances(tmp_path)
    result = install_digital_human(
        s, {}, task_store=ts, scratch_provider=lambda sid: sid, instances=insts
    )
    inst_id = result["instance"]["id"]
    task_id = result["instance"]["task_id"]
    assert uninstall_digital_human(inst_id, instances=insts, task_store=ts)["ok"]
    assert insts.get(inst_id) is None
    assert ts.get(task_id) is None


def test_instance_store_persistence_roundtrip(tmp_path):
    store = _make_instances(tmp_path)
    inst = DigitalHumanInstance(
        id="dh-1", slug="news", name="News", task_id="task-1",
        config={"topic": "AI"}, secret_keys=["api_key"], spec_version="1.0.0",
    )
    store.put(inst)
    # Re-open from disk.
    store2 = InstanceStore(tmp_path / "dh.json")
    assert store2.get("dh-1") is not None
    assert store2.get("dh-1").config == {"topic": "AI"}
    assert store2.list()[0].slug == "news"


# -- registry + real DHP repo (skip if absent) --------------------------------


@pytest.mark.skipif(not DHP_REPO.is_dir(), reason="DHP repo not cloned")
def test_registry_loads_real_index():
    reg = DhpRegistry(DHP_REPO)
    assert len(reg) >= 30
    cats = reg.categories()
    assert "content" in cats or "social" in cats


@pytest.mark.skipif(not DHP_REPO.is_dir(), reason="DHP repo not cloned")
def test_registry_parses_all_real_specs():
    from croniter import croniter

    reg = DhpRegistry(DHP_REPO)
    failures = []
    for slug in reg.slugs():
        try:
            s = reg.get_spec(slug)
            assert s.type in ("automation", "skill", "mcp", "extension")
            if s.primary_schedule and s.primary_schedule.cron:
                assert croniter.is_valid(s.primary_schedule.cron), f"{slug}: bad cron {s.primary_schedule.cron}"
        except Exception as e:
            failures.append((slug, str(e)))
    assert not failures, f"spec parse failures: {failures}"


# -- source management (批次 D2) ---------------------------------------------


def test_source_manager_seeds_builtins_when_empty(tmp_path):
    from coworker.digital_human.sources import SourceManager, BUILTIN_SOURCES

    prefs: dict = {}
    saved = []
    sm = SourceManager(prefs, lambda: saved.append("saved"))
    sm.ensure_builtins()
    assert prefs["dhp_sources"], "builtin source should be persisted"
    assert prefs["dhp_sources"][0]["id"] == BUILTIN_SOURCES[0].id
    assert prefs["dhp_sources"][0]["is_default"] is True
    assert saved  # save was called


def test_source_manager_builtin_deletable_and_resettable(tmp_path):
    from coworker.digital_human.sources import SourceManager

    prefs: dict = {}
    sm = SourceManager(prefs, lambda: None)
    sm.ensure_builtins()
    builtin_id = sm.list()[0].id
    # Builtins are deletable (user chose "deleted means deleted"). The deletion is recorded
    # so ensure_builtins() doesn't re-assert it.
    assert sm.remove(builtin_id) is True
    assert all(s.id != builtin_id for s in sm.list())
    sm.ensure_builtins()
    assert all(s.id != builtin_id for s in sm.list())
    # The deleted-builtin record is persisted in prefs.
    assert builtin_id in (prefs.get("deleted_builtin_dhp_sources") or [])
    # reset() restores all builtins (the "reset to defaults" escape hatch).
    sources = sm.reset()
    assert any(s.id == builtin_id for s in sources)


def test_source_manager_add_toggle_remove_custom(tmp_path):
    from coworker.digital_human.sources import SourceManager

    prefs: dict = {}
    sm = SourceManager(prefs, lambda: None)
    sm.ensure_builtins()
    src = sm.add("My Mirror", "https://example.com/dhp")
    assert src.id != "dhp-official"
    # Toggle off.
    sm.update(src.id, {"enabled": False})
    assert not sm.list()[0].enabled or not next(s for s in sm.list() if s.id == src.id).enabled
    # Custom source can be removed.
    assert sm.remove(src.id) is True
    assert all(s.id != src.id for s in sm.list())


def test_source_manager_ensure_builtins_preserves_disabled_preference(tmp_path):
    from coworker.digital_human.sources import SourceManager

    prefs: dict = {}
    sm = SourceManager(prefs, lambda: None)
    sm.ensure_builtins()
    builtin_id = sm.list()[0].id
    # User disables the builtin.
    sm.update(builtin_id, {"enabled": False})
    # A second ensure_builtins (e.g. restart) keeps it disabled.
    sm2 = SourceManager(prefs, lambda: None)
    sm2.ensure_builtins()
    assert next(s for s in sm2.list() if s.id == builtin_id).enabled is False


def test_source_manager_add_rejects_empty_fields():
    from coworker.digital_human.sources import SourceManager

    sm = SourceManager({}, lambda: None)
    with pytest.raises(ValueError):
        sm.add("", "https://x")
    with pytest.raises(ValueError):
        sm.add("name", "")


# -- HTTP adapter (mocked httpx, no real network) -----------------------------


class _FakeResponse:
    def __init__(self, *, json_data=None, text=None, status=200):
        self._json = json_data
        self.text = text or ""
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakeHttpxClient:
    """Captures the urls requested and returns scripted responses."""

    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses
        self.requested: list[str] = []

    def get(self, url):
        self.requested.append(url)
        return self._responses.get(url, _FakeResponse(status=404, text="not found"))


def test_http_adapter_fetch_index_and_spec(monkeypatch):
    from coworker.digital_human.adapters import DhpHttpAdapter

    index_url = "https://example.com/dhp/index.json"
    spec_url = "https://example.com/dhp/packages/digital-humans/foo/spec.yaml"
    fake = _FakeHttpxClient(
        {
            index_url: _FakeResponse(
                json_data={"apps": [{"slug": "foo", "name": "Foo", "path": "packages/digital-humans/foo"}]}
            ),
            spec_url: _FakeResponse(
                text="name: Foo\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: hi\nsubscriptions: []\n"
            ),
        }
    )
    monkeypatch.setattr(
        "coworker.digital_human.adapters.httpx", type("M", (), {"Client": lambda *a, **k: fake})
    )
    adapter = DhpHttpAdapter("https://example.com/dhp")
    entries = adapter.fetch_index()
    assert len(entries) == 1 and entries[0].slug == "foo"
    spec = adapter.fetch_spec("foo", "packages/digital-humans/foo")
    assert spec.name == "Foo"
    assert spec.system_prompt.strip() == "hi"


def test_http_adapter_network_failure_raises_on_first_load(monkeypatch):
    from coworker.digital_human.adapters import DhpHttpAdapter

    class _BoomClient:
        def get(self, url):
            raise ConnectionError("no network")

    monkeypatch.setattr(
        "coworker.digital_human.adapters.httpx", type("M", (), {"Client": lambda *a, **k: _BoomClient()})
    )
    adapter = DhpHttpAdapter("https://example.com/dhp")
    # First load with no cache → raises (the registry records this so the UI shows a diagnostic
    # instead of a silently-empty store).
    with pytest.raises(RuntimeError, match="no network"):
        adapter.fetch_index()


def test_assert_safe_rel_path_rejects_traversal():
    from coworker.digital_human.adapters import assert_safe_rel_path

    assert assert_safe_rel_path("packages/digital-humans/foo") == "packages/digital-humans/foo"
    with pytest.raises(SpecError):
        assert_safe_rel_path("../etc/passwd")
    with pytest.raises(SpecError):
        assert_safe_rel_path("/abs/path")
    with pytest.raises(SpecError):
        assert_safe_rel_path("a/../../b")


def test_http_adapter_strips_index_json_suffix(monkeypatch):
    """A source URL ending in /index.json must not double the suffix.

    fetch_index joins ``{self.url}/index.json``; a source URL that already ends in the
    file name would request ``…/index.json/index.json`` and 404 silently — the empty-store
    root cause. The adapter strips the suffix so both the directory and full-file forms work.
    """
    from coworker.digital_human.adapters import DhpHttpAdapter, _strip_index_suffix

    # The suffix-stripping helper covers the directory, full-file, and trailing-slash forms.
    # It strips only the index suffix; a residual trailing slash is cleaned by the adapter ctor.
    assert _strip_index_suffix("https://x.io/dhp") == "https://x.io/dhp"
    assert _strip_index_suffix("https://x.io/dhp/index.json") == "https://x.io/dhp"
    assert _strip_index_suffix("https://x.io/dhp/index.JSON") == "https://x.io/dhp"
    assert _strip_index_suffix("https://x.io/dhp/index.yml/") == "https://x.io/dhp/"
    assert _strip_index_suffix("https://x.io/dhp/") == "https://x.io/dhp/"

    # End-to-end: an adapter built from the full-file URL fetches the directory's index.json,
    # not a doubled path. The fake client records the requested URL to prove it.
    captured: list[str] = []
    index_url = "https://x.io/dhp/index.json"

    class _Client:
        def get(self, url):
            captured.append(url)
            if url == index_url:
                return _FakeResponse(json_data={"apps": [{"slug": "foo"}]})
            return _FakeResponse(status=404)

    monkeypatch.setattr(
        "coworker.digital_human.adapters.httpx", type("M", (), {"Client": lambda *a, **k: _Client()})
    )
    # Source URL points at the index file itself (what a user pastes) — must still resolve.
    adapter = DhpHttpAdapter("https://x.io/dhp/index.json")
    entries = adapter.fetch_index()
    assert len(entries) == 1 and entries[0].slug == "foo"
    # The request hit the canonical index URL, not a doubled path.
    assert captured == [index_url]


# -- multi-source registry ----------------------------------------------------


def test_registry_merges_multiple_sources(tmp_path):
    from coworker.digital_human.sources import RegistrySource
    from coworker.digital_human.store import DhpRegistry

    # Two local sources, each with one entry.
    src_a = tmp_path / "a"
    src_b = tmp_path / "b"
    for src, slug in [(src_a, "alpha"), (src_b, "beta")]:
        (src / "packages" / "digital-humans" / slug).mkdir(parents=True)
        (src / "packages" / "digital-humans" / slug / "spec.yaml").write_text(
            f"name: {slug}\nversion: '1'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: x\nsubscriptions: []\n",
            encoding="utf-8",
        )
        (src / "index.json").write_text(
            json.dumps({"apps": [{"slug": slug, "name": slug, "path": f"packages/digital-humans/{slug}"}]}),
            encoding="utf-8",
        )
    reg = DhpRegistry(
        [
            RegistrySource(id="a", name="A", url=str(src_a), source_type="local"),
            RegistrySource(id="b", name="B", url=str(src_b), source_type="local"),
        ]
    )
    assert set(reg.slugs()) == {"alpha", "beta"}
    assert reg.get_spec("alpha").name == "alpha"
    assert reg.get_spec("beta").name == "beta"


def test_registry_disabled_source_skipped(tmp_path):
    from coworker.digital_human.sources import RegistrySource
    from coworker.digital_human.store import DhpRegistry

    src_a = tmp_path / "a"
    (src_a / "packages" / "digital-humans" / "alpha").mkdir(parents=True)
    (src_a / "packages" / "digital-humans" / "alpha" / "spec.yaml").write_text(
        "name: alpha\nversion: '1'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: x\nsubscriptions: []\n",
        encoding="utf-8",
    )
    (src_a / "index.json").write_text(
        json.dumps({"apps": [{"slug": "alpha", "name": "alpha", "path": "packages/digital-humans/alpha"}]}),
        encoding="utf-8",
    )
    reg = DhpRegistry(
        [RegistrySource(id="a", name="A", url=str(src_a), source_type="local", enabled=False)]
    )
    assert reg.slugs() == []  # disabled source contributes nothing


# -- update_digital_human (编辑链路) -----------------------------------------


class _FakeManager:
    """A minimal manager exposing only what update_digital_human needs (avoids the full SessionManager)."""

    def __init__(self, tmp_path):
        from coworker.secrets import SecretStore

        self.task_store = _FakeTaskStore()
        self.secrets = SecretStore(tmp_path / "secrets.json")
        self.dhp_instances = InstanceStore(tmp_path / "dh.json", secrets=self.secrets)
        # preflight_digital_human checks whether requires_rules/requires_hooks are
        # already registered against the rule/hook stores. Bind real stores on an
        # empty prefs dict so the soft-dep probe works under the fake.
        from coworker.rules import RuleStore
        from coworker.hooks import HookStore

        self._prefs: dict = {}
        self._save_prefs = lambda: None
        self.rule_store = RuleStore(self._prefs, self._save_prefs)
        self.hooks = HookStore(self._prefs, self._save_prefs)
        # Build a registry from a local source with one spec.
        repo = tmp_path / "repo"
        (repo / "packages" / "digital-humans" / "news").mkdir(parents=True)
        (repo / "packages" / "digital-humans" / "news" / "spec.yaml").write_text(
            "name: News\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: Report news.\n"
            "store:\n  slug: news\n"
            "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 24h\n"
            "config_schema:\n  - key: topic\n    label: Topic\n    type: string\n    default: AI\n"
            "  - key: api_key\n    label: K\n    type: string\n",
            encoding="utf-8",
        )
        (repo / "index.json").write_text(
            json.dumps({"apps": [{"slug": "news", "name": "News", "path": "packages/digital-humans/news"}]}),
            encoding="utf-8",
        )
        from coworker.digital_human.sources import RegistrySource

        self.dhp_registry = DhpRegistry([RegistrySource(id="local", name="L", url=str(repo), source_type="local")])
        # update_digital_human calls self.update_automation — wire to the real SessionManager method.
        from coworker.server.manager import SessionManager

        self.update_automation = SessionManager.update_automation.__get__(self)
        # install_digital_human calls self.preflight_digital_human to recompute the digest at the
        # approval boundary — bind the real method so the install path works under the fake.
        self.preflight_digital_human = SessionManager.preflight_digital_human.__get__(self)
        # install_digital_human also calls self._provision_scratch to get a workspace dir. The
        # fake doesn't manage real sessions, so a fixed temp dir under the workspace suffices.
        self._scratch_dir = tmp_path / "scratch"
        self._scratch_dir.mkdir(exist_ok=True)

    def _provision_scratch(self, session_id):
        return str(self._scratch_dir / session_id)


def test_update_digital_human_changes_cron(tmp_path):
    from coworker.digital_human import install_digital_human
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    spec = mgr.dhp_registry.get_spec("news")
    inst_result = install_digital_human(
        spec, {}, task_store=mgr.task_store, scratch_provider=lambda sid: sid, instances=mgr.dhp_instances
    )
    assert inst_result["ok"]
    inst_id = inst_result["instance"]["id"]

    result = SessionManager.update_digital_human(mgr, inst_id, {"cron": "0 9 * * *"})
    assert result["ok"], result
    task = mgr.task_store.get(inst_result["instance"]["task_id"])
    assert task.schedule.cron == "0 9 * * *"


def test_update_digital_human_changes_system_prompt(tmp_path):
    from coworker.digital_human import install_digital_human
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    spec = mgr.dhp_registry.get_spec("news")
    inst_result = install_digital_human(
        spec, {}, task_store=mgr.task_store, scratch_provider=lambda sid: sid, instances=mgr.dhp_instances
    )
    inst_id = inst_result["instance"]["id"]

    new_prompt = "You are a rewritten agent. Do better things."
    result = SessionManager.update_digital_human(mgr, inst_id, {"system_prompt": new_prompt})
    assert result["ok"], result
    task = mgr.task_store.get(inst_result["instance"]["task_id"])
    # Prompt rebuilt with preamble preserved.
    assert "## 用户配置" in task.instructions
    assert new_prompt in task.instructions
    assert "Report news." not in task.instructions  # old prompt replaced


def test_update_digital_human_changes_config_and_secret(tmp_path):
    from coworker.digital_human import install_digital_human
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    spec = mgr.dhp_registry.get_spec("news")
    inst_result = install_digital_human(
        spec, {}, task_store=mgr.task_store, scratch_provider=lambda sid: sid, instances=mgr.dhp_instances
    )
    inst_id = inst_result["instance"]["id"]

    result = SessionManager.update_digital_human(
        mgr, inst_id, {"user_config": {"topic": "sports", "api_key": "sk-new"}}
    )
    assert result["ok"], result
    inst = mgr.dhp_instances.get(inst_id)
    assert inst.config["topic"] == "sports"
    assert "api_key" not in inst.config  # secret stays out of plaintext
    assert inst.secret_keys == ["api_key"]
    secret_data = mgr.secrets.get(inst.secret_profile())
    assert secret_data["api_key"] == "sk-new"


def test_update_digital_human_unknown_instance_fails(tmp_path):
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    result = SessionManager.update_digital_human(mgr, "nope", {"cron": "0 0 * * *"})
    assert not result["ok"]
    assert "not found" in result["error"]


# -- upgrade check ------------------------------------------------------------


def test_upgrade_check_detects_outdated(tmp_path):
    from coworker.digital_human import install_digital_human
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    spec = mgr.dhp_registry.get_spec("news")
    inst_result = install_digital_human(
        spec, {}, task_store=mgr.task_store, scratch_provider=lambda sid: sid, instances=mgr.dhp_instances
    )
    inst_id = inst_result["instance"]["id"]
    # Bump the registry version after install.
    spec_yaml = mgr.dhp_registry.get_spec("news")
    original = spec_yaml.version
    # Simulate a newer version in the index entry without touching the spec file.
    mgr.dhp_registry._ensure_loaded()
    _, entry = mgr.dhp_registry._entry_index["news"]
    entry.version = "2.0.0"
    result = SessionManager.dhp_upgrade_check(mgr, inst_id)
    assert result["ok"]
    assert result["installed_version"] == "1.0.0"
    assert result["latest_version"] == "2.0.0"
    assert result["up_to_date"] is False



# -- secret boundary (Task #11: DHP hardening) --------------------------------


def test_build_instructions_masks_secret_values():
    """Secret config values must NEVER appear in the static task instructions."""
    s = _skill_spec(
        "config_schema:\n  - key: api_key\n    label: K\n    type: string\n"
        "  - key: topic\n    label: T\n    type: string\n"
    )
    out = build_instructions(s, {"topic": "news"}, secret_keys=["api_key"])
    assert "<configured>" in out
    assert "topic" in out and "news" in out  # non-secret value is present
    # No real secret value leaks even if a caller mistakenly passes it.
    out2 = build_instructions(s, {"topic": "news", "api_key": "sk-LEAK-VALUE"})
    assert "sk-LEAK-VALUE" not in out2
    assert "<configured>" in out2


def test_install_instructions_have_no_secret_value(tmp_path):
    """The installed task's instructions carry a marker, not the secret value."""
    from coworker.secrets import SecretStore

    s = parse_spec(
        "name: X\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y.\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 1h\n"
        "config_schema:\n  - key: api_token\n    label: T\n    type: string\n    required: true\n"
        "  - key: topic\n    label: Topic\n    type: string\n    default: news\n"
    )
    ts = _FakeTaskStore()
    secrets = SecretStore(tmp_path / "secrets.json")
    insts = InstanceStore(tmp_path / "dh.json", secrets=secrets)
    result = install_digital_human(
        s, {"api_token": "sk-SECRET-123"},
        task_store=ts, scratch_provider=lambda sid: sid, instances=insts,
    )
    assert result["ok"]
    task = ts.get(result["instance"]["task_id"])
    assert "sk-SECRET-123" not in task.instructions
    assert "<configured>" in task.instructions
    # The secret value IS in the SecretStore (resolvable at run time by the capability layer).
    assert secrets.get(insts.get(result["instance"]["id"]).secret_profile())["api_token"] == "sk-SECRET-123"


def test_reinstall_instructions_masks_secret_on_edit(tmp_path):
    """Editing an instance must not leak the secret into the rewritten instructions."""
    from coworker.digital_human import reinstall_instructions
    from coworker.secrets import SecretStore

    s = parse_spec(
        "name: X\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y.\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 1h\n"
        "config_schema:\n  - key: api_key\n    label: K\n    type: string\n"
        "  - key: topic\n    label: T\n    type: string\n"
    )
    out = reinstall_instructions(s, {"topic": "AI"}, system_prompt="custom prompt", secret_keys=["api_key"])
    assert "<configured>" in out
    assert "custom prompt" in out
    assert "api_key" in out  # the KEY is present (as a marker), the VALUE is not
    # No leak even if a value slips through.
    out2 = reinstall_instructions(s, {"topic": "AI", "api_key": "sk-LEAK"})
    assert "sk-LEAK" not in out2


def test_fresh_install_clears_migration_flag(tmp_path):
    """A fresh install is clean — needs_secret_migration is False."""
    from coworker.secrets import SecretStore

    s = parse_spec(
        "name: X\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: y.\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 1h\n"
    )
    ts = _FakeTaskStore()
    insts = InstanceStore(tmp_path / "dh.json", secrets=SecretStore(tmp_path / "s.json"))
    result = install_digital_human(
        s, {}, task_store=ts, scratch_provider=lambda sid: sid, instances=insts,
    )
    inst = insts.get(result["instance"]["id"])
    assert inst.needs_secret_migration is False
    assert inst.unreviewed is False
    assert result["instance"]["needs_secret_migration"] is False


def test_legacy_instance_marks_migration_needed(tmp_path):
    """An instance record loaded from a legacy file (no migration flag) is treated as needing it."""
    from coworker.digital_human.instances import InstanceStore, DigitalHumanInstance

    path = tmp_path / "dh.json"
    # Write a legacy record that predates the secret-boundary fix (no needs_secret_migration key).
    path.write_text(json.dumps({"instances": [{
        "id": "dh-legacy", "slug": "news", "name": "News", "task_id": "t-1",
        "config": {"topic": "AI"}, "secret_keys": ["api_key"], "spec_version": "1.0.0",
    }]}), encoding="utf-8")
    store = InstanceStore(path)
    inst = store.get("dh-legacy")
    assert inst.needs_secret_migration is True  # legacy → flagged


def test_edit_clears_migration_flag(tmp_path):
    """Re-saving an instance (config/prompt edit) rewrites clean instructions → flag clears."""
    from coworker.digital_human import install_digital_human
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    spec = mgr.dhp_registry.get_spec("news")
    result = install_digital_human(
        spec, {}, task_store=mgr.task_store, scratch_provider=lambda sid: sid, instances=mgr.dhp_instances
    )
    inst_id = result["instance"]["id"]
    # Force the legacy flag on, then re-save via an edit.
    mgr.dhp_instances.get(inst_id).needs_secret_migration = True
    SessionManager.update_digital_human(mgr, inst_id, {"user_config": {"topic": "sports"}})
    inst = mgr.dhp_instances.get(inst_id)
    assert inst.needs_secret_migration is False
    # And the rewritten task instructions carry no plaintext secret.
    task = mgr.task_store.get(inst.task_id)
    assert "<configured>" in task.instructions or "api_key" not in task.instructions


# -- dependency approval digest (Task #11) ------------------------------------


def test_preflight_returns_manifest_and_digest(tmp_path):
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    pf = SessionManager.preflight_digital_human(mgr, "news", {})
    assert pf["ok"], pf
    assert "approval_digest" in pf and len(pf["approval_digest"]) == 64
    m = pf["manifest"]
    assert m["version"] == "1.0.0"
    assert "config_secret_keys" in m
    assert "api_key" in m["config_secret_keys"]


def test_install_without_digest_is_refused(tmp_path):
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    result = SessionManager.install_digital_human(mgr, "news", {})
    assert not result["ok"]
    assert "re-approve" in result["error"] or "approval" in result["error"].lower()
    assert "approval_digest" in result  # caller gets the digest to show + resubmit


# -- requires_rules / requires_hooks soft-dep preflight (Task #31) ------------


def test_preflight_rules_and_hooks_soft_deps(tmp_path):
    """requires_rules/requires_hooks are soft deps: preflight reports each as
    configured:True/False and surfaces unconfigured ones in needs_attention,
    but never blocks install (the manifest is still ok with a valid digest)."""
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    # A spec that expects one rule + one hook.
    repo = tmp_path / "repo2"
    (repo / "packages" / "digital-humans" / "guarded").mkdir(parents=True)
    (repo / "packages" / "digital-humans" / "guarded" / "spec.yaml").write_text(
        "name: Guarded\nversion: '1.0.0'\nauthor: a\ndescription: d\ntype: automation\nsystem_prompt: x.\n"
        "store:\n  slug: guarded\n"
        "subscriptions:\n  - source:\n      type: schedule\n      config:\n        every: 24h\n"
        "requires:\n"
        "  rules:\n"
        "    - pattern: write_file\n      action: deny\n      reason: read-only DH\n"
        "  hooks:\n"
        "    - event: pre_tool\n      command: audit.sh\n      match_tool: write_*\n",
        encoding="utf-8",
    )
    (repo / "index.json").write_text(
        json.dumps({"apps": [{"slug": "guarded", "name": "Guarded", "path": "packages/digital-humans/guarded"}]}),
        encoding="utf-8",
    )
    from coworker.digital_human.sources import RegistrySource
    from coworker.digital_human.store import DhpRegistry

    mgr.dhp_registry = DhpRegistry([RegistrySource(id="local", name="L", url=str(repo), source_type="local")])

    pf = SessionManager.preflight_digital_human(mgr, "guarded", {})
    assert pf["ok"], pf
    m = pf["manifest"]
    assert len(m["requires_rules"]) == 1
    assert m["requires_rules"][0]["pattern"] == "write_file"
    assert m["requires_rules"][0]["action"] == "deny"
    assert m["requires_rules"][0]["configured"] is False
    assert len(m["requires_hooks"]) == 1
    assert m["requires_hooks"][0]["event"] == "pre_tool"
    assert m["requires_hooks"][0]["match_tool"] == "write_*"
    assert m["requires_hooks"][0]["configured"] is False
    # Both surface in needs_attention as unconfigured soft gaps.
    assert any("write_file" in n and "deny" in n for n in pf["needs_attention"])
    assert any("pre_tool" in n for n in pf["needs_attention"])

    # Register the matching rule + hook, re-preflight → both configured, no attention.
    mgr.rule_store.add(pattern="write_file", action="deny")
    mgr.hooks.add("audit", "pre_tool", "audit.sh", match="*", match_tool="write_*")
    pf2 = SessionManager.preflight_digital_human(mgr, "guarded", {})
    assert pf2["ok"], pf2
    assert pf2["manifest"]["requires_rules"][0]["configured"] is True
    assert pf2["manifest"]["requires_hooks"][0]["configured"] is True
    assert pf2["needs_attention"] == []


def test_install_with_matching_digest_succeeds(tmp_path):
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    pf = SessionManager.preflight_digital_human(mgr, "news", {})
    result = SessionManager.install_digital_human(mgr, "news", {}, approval_digest=pf["approval_digest"])
    assert result["ok"], result


def test_install_with_stale_digest_is_refused(tmp_path):
    from coworker.server.manager import SessionManager

    mgr = _FakeManager(tmp_path)
    # A digest that doesn't match the current manifest.
    result = SessionManager.install_digital_human(mgr, "news", {}, approval_digest="0" * 64)
    assert not result["ok"]
    assert "changed" in result["error"] or "re-approve" in result["error"]


# -- aisuite.agents compatibility shim ---------------------------------------


def test_aisuite_compat_shim_provides_tool_and_metadata():
    """The shim exports ``tool`` + ``ToolMetadata`` whether or not the pinned aisuite commit is
    installed. ``tool`` must attach ``__aisuite_tool_metadata__`` so the ToolRegistry can read it.
    """
    from coworker.tools._aisuite_compat import ToolMetadata, tool

    meta = ToolMetadata(category="interaction", risk_level="low",
                       capabilities=["x"], description="d")

    @tool
    def my_tool(question: str) -> dict:
        return {"answer": ""}

    tool_with_meta = tool(my_tool, metadata=meta)
    # tool() returns the callable (registry calls it directly + reads __name__).
    assert callable(tool_with_meta)
    assert getattr(tool_with_meta, "__name__", None) == "my_tool"
    # metadata attached so ToolRegistry.register can pick it up.
    assert getattr(tool_with_meta, "__aisuite_tool_metadata__", None) is meta


def test_aisuite_compat_shim_real_tools_still_importable():
    """The shim does not break the real tool factories (ask/directories/plan) that depend on it."""
    from coworker.tools.ask import ask_user_tool
    from coworker.tools.directories import request_directory_tool
    from coworker.tools.plan import propose_plan_tool
    assert callable(ask_user_tool())
    assert callable(request_directory_tool())
    assert callable(propose_plan_tool())
