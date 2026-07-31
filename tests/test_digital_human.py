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

DHP_REPO = Path(os.environ.get("OPENWORKER_DHP_REPO", "")).resolve() or Path(
    __file__
).resolve().parents[2] / "digital-human-protocol"


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
