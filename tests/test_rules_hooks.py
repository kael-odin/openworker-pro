"""Rules (allow/deny/ask) + Hooks (pre_run/post_run) — 批次 E2."""

from __future__ import annotations

import json
import sys

from coworker.hooks import EVENTS, HookStore, POST_RUN, PRE_RUN
from coworker.permissions import PermissionEngine, Decision
from coworker.risk import RiskClass
from coworker.rules import ACTIONS, ALLOW, ASK, DENY, RuleStore


# -- RuleStore (prefs persistence, CRUD, resolution) --------------------------


def _prefs_and_store():
    prefs: dict = {}
    save = lambda: None  # noqa: E731 — in-memory; save is a no-op
    return prefs, RuleStore(prefs, save)


def test_rule_store_add_and_list():
    _prefs, store = _prefs_and_store()
    r = store.add("mcp__notion__*", ASK, reason="confirm notion writes")
    assert r["action"] == ASK
    assert r["pattern"] == "mcp__notion__*"
    listed = store.list()
    assert len(listed) == 1
    assert listed[0]["pattern"] == "mcp__notion__*"
    assert listed[0]["id"]  # id assigned


def test_rule_store_add_rejects_invalid_action():
    _prefs, store = _prefs_and_store()
    try:
        store.add("run_shell", "block")  # not in ACTIONS
    except ValueError:
        return
    assert False, "expected ValueError for invalid action"


def test_rule_store_add_rejects_empty_pattern():
    _prefs, store = _prefs_and_store()
    try:
        store.add("   ", ALLOW)
    except ValueError:
        return
    assert False, "expected ValueError for empty pattern"


def test_rule_store_add_dedupes_by_pattern():
    _prefs, store = _prefs_and_store()
    r1 = store.add("run_shell", ASK)
    r2 = store.add("run_shell", DENY)  # same pattern → replace, keep id
    assert r1["id"] == r2["id"]
    assert r2["action"] == DENY
    assert len(store.list()) == 1  # not 2


def test_rule_store_update_and_remove():
    _prefs, store = _prefs_and_store()
    r = store.add("write_file", ALLOW)
    updated = store.update(r["id"], {"action": DENY, "reason": "blocked"})
    assert updated["action"] == DENY
    assert updated["reason"] == "blocked"
    assert store.remove(r["id"]) is True
    assert store.list() == []
    # Removing again is a no-op (False).
    assert store.remove(r["id"]) is False


def test_rule_store_update_unknown_id_returns_none():
    _prefs, store = _prefs_and_store()
    assert store.update("nope", {"action": ALLOW}) is None


def test_rule_store_resolve_most_specific_wins():
    _prefs, store = _prefs_and_store()
    store.add("mcp__*", ALLOW)  # broad glob
    store.add("mcp__notion__*", ASK)  # more specific
    store.add("mcp__notion__delete_page", DENY)  # exact (most specific)
    # Exact beats glob.
    assert store.resolve("mcp__notion__delete_page") == DENY
    # More specific glob beats broad glob.
    assert store.resolve("mcp__notion__create_page") == ASK
    # Broad glob matches the rest.
    assert store.resolve("mcp__slack__send_msg") == ALLOW
    # No match → None (defer to base risk).
    assert store.resolve("write_file") is None


def test_rule_store_resolve_skips_disabled():
    _prefs, store = _prefs_and_store()
    store.add("run_shell", DENY)
    store.update(store.list()[0]["id"], {"enabled": False})
    assert store.resolve("run_shell") is None  # disabled rule doesn't apply
    assert store.list(enabled_only=True) == []


def test_rule_store_persisted_in_prefs_key():
    prefs, store = _prefs_and_store()
    store.add("run_shell", ASK)
    # The store writes to prefs["rules"], so a fresh store on the same prefs sees it.
    store2 = RuleStore(prefs, lambda: None)
    assert len(store2.list()) == 1
    assert store2.list()[0]["pattern"] == "run_shell"


def test_rule_store_resolver_callable():
    _prefs, store = _prefs_and_store()
    store.add("run_shell", DENY)
    fn = store.resolver()
    assert callable(fn)
    assert fn("run_shell") == DENY
    assert fn("write_file") is None


def test_actions_constant():
    assert ACTIONS == (ALLOW, DENY, ASK)


# -- HookStore (prefs persistence, CRUD, firing) ------------------------------


def _prefs_and_hook_store():
    prefs: dict = {}
    save = lambda: None  # noqa: E731
    return prefs, HookStore(prefs, save)


def test_hook_store_add_and_list():
    _prefs, store = _prefs_and_hook_store()
    h = store.add("notify", POST_RUN, "echo done", match="*")
    assert h["event"] == POST_RUN
    assert h["command"] == "echo done"
    assert h["match"] == "*"
    assert h["id"]
    listed = store.list()
    assert len(listed) == 1
    assert listed[0]["name"] == "notify"


def test_hook_store_add_rejects_invalid_event():
    _prefs, store = _prefs_and_hook_store()
    try:
        store.add("x", "pre_tool", "echo")
    except ValueError:
        return
    assert False, "expected ValueError for invalid event"


def test_hook_store_add_rejects_empty_command():
    _prefs, store = _prefs_and_hook_store()
    try:
        store.add("x", PRE_RUN, "   ")
    except ValueError:
        return
    assert False, "expected ValueError for empty command"


def test_hook_store_update_and_remove():
    _prefs, store = _prefs_and_hook_store()
    h = store.add("n", PRE_RUN, "echo pre")
    updated = store.update(h["id"], {"command": "echo new", "enabled": False})
    assert updated["command"] == "echo new"
    assert updated["enabled"] is False
    assert store.remove(h["id"]) is True
    assert store.list() == []


def test_hook_store_persisted_in_prefs_key():
    prefs, store = _prefs_and_hook_store()
    store.add("n", POST_RUN, "echo done")
    store2 = HookStore(prefs, lambda: None)
    assert len(store2.list()) == 1


def test_events_constant():
    assert EVENTS == (PRE_RUN, POST_RUN)


def test_hook_fire_runs_matching_hook_and_returns_result():
    _prefs, store = _prefs_and_hook_store()
    # A hook that echoes a fixed string on post_run.
    store.add("echoer", POST_RUN, 'echo {"ran": true}', match="*")
    results = store.fire(POST_RUN, {"task_name": "Daily Report", "event": POST_RUN})
    assert len(results) == 1
    assert results[0]["name"] == "echoer"
    assert results[0]["ok"] is True
    assert results[0]["returncode"] == 0


def test_hook_fire_skips_non_matching_event():
    _prefs, store = _prefs_and_hook_store()
    store.add("pre", PRE_RUN, "echo pre", match="*")
    # Firing post_run should not trigger the pre_run hook.
    results = store.fire(POST_RUN, {"task_name": "x", "event": POST_RUN})
    assert results == []


def test_hook_fire_skips_non_matching_name():
    _prefs, store = _prefs_and_hook_store()
    store.add(" selective", POST_RUN, "echo done", match="Important*")
    # Non-matching task name → hook doesn't fire.
    assert store.fire(POST_RUN, {"task_name": "Daily", "event": POST_RUN}) == []
    # Matching task name → fires.
    results = store.fire(POST_RUN, {"task_name": "Important Job", "event": POST_RUN})
    assert len(results) == 1


def test_hook_fire_skips_disabled():
    _prefs, store = _prefs_and_hook_store()
    h = store.add("off", POST_RUN, "echo done")
    store.update(h["id"], {"enabled": False})
    assert store.fire(POST_RUN, {"task_name": "x", "event": POST_RUN}) == []


def test_hook_fire_never_raises_on_bad_command():
    _prefs, store = _prefs_and_hook_store()
    # A command that doesn't exist — should record failure, not raise.
    store.add("bad", POST_RUN, "this-command-does-not-exist-xyz", match="*")
    results = store.fire(POST_RUN, {"task_name": "x", "event": POST_RUN})
    assert len(results) == 1
    # shell=True returns non-zero for a missing command; ok=False but no exception.
    assert results[0]["ok"] is False


def test_hook_fire_pre_run_skip_signal():
    _prefs, store = _prefs_and_hook_store()
    # A pre_run hook that requests a skip via stdout JSON. printf keeps the JSON
    # quotes intact (a bare `echo {"skip": true}` loses its quotes under sh).
    store.add("guard", PRE_RUN, 'printf \'%s\' \'{"skip": true}\'', match="*")
    results = store.fire(PRE_RUN, {"task_name": "x", "event": PRE_RUN})
    assert len(results) == 1
    assert results[0].get("skip") is True


def test_hook_fire_passes_context_on_stdin():
    _prefs, store = _prefs_and_hook_store()
    # Python one-liner that reads stdin JSON and echoes the task_name back.
    py = sys.executable.replace("\\", "/")
    store.add(
        "ctx",
        POST_RUN,
        f'"{py}" -c "import sys,json; d=json.load(sys.stdin); print(d[\\"task_name\\"])"',
        match="*",
    )
    results = store.fire(POST_RUN, {"task_name": "My Task", "event": POST_RUN})
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert "My Task" in results[0]["stdout"]


# -- Tolerant loading (malformed entries skipped) -----------------------------


def test_rule_store_tolerates_malformed_entries():
    prefs = {"rules": [{"pattern": "ok", "action": "allow"}, {"missing_action": True}, "not-a-dict"]}
    store = RuleStore(prefs, lambda: None)
    listed = store.list()
    assert len(listed) == 1  # only the well-formed rule survives
    assert listed[0]["pattern"] == "ok"


def test_hook_store_tolerates_malformed_entries():
    prefs = {"hooks": [{"name": "ok", "event": "post_run", "command": "echo"}, {"no_name": True}]}
    store = HookStore(prefs, lambda: None)
    listed = store.list()
    assert len(listed) == 1
    assert listed[0]["name"] == "ok"


# -- E2.5: RuleStore wired into PermissionEngine (rules actually take effect) --
# Rules are the highest-precedence layer: checked before risk classification.

from types import SimpleNamespace  # noqa: E402

from coworker.permissions import Mode  # noqa: E402

MCP_META = SimpleNamespace(requires_approval=True, category="mcp")


def test_rule_deny_blocks_before_risk_classification(tmp_path):
    # An MCP tool is external-risk (needs approval) by default; a deny rule blocks it
    # outright — no approval path, not even needs_user.
    prefs = {}
    store = RuleStore(prefs, lambda: None)
    store.add("mcp__notion__delete_page", DENY)
    eng = PermissionEngine(workspace_root=tmp_path, rule_resolver=store.resolver())
    d = eng.evaluate("mcp__notion__delete_page", {}, MCP_META)
    assert d.allowed is False
    assert d.needs_user is False  # deny is a hard block, not an approval request


def test_rule_allow_bypasses_approval(tmp_path):
    # An MCP tool that would normally gate (external risk) is auto-allowed by a rule.
    prefs = {}
    store = RuleStore(prefs, lambda: None)
    store.add("mcp__notion__get_page", ALLOW)
    eng = PermissionEngine(workspace_root=tmp_path, rule_resolver=store.resolver())
    d = eng.evaluate("mcp__notion__get_page", {}, MCP_META)
    assert d.allowed is True
    assert d.needs_user is False


def test_rule_ask_forces_approval_even_for_read_tools(tmp_path):
    # read_file is normally auto-allowed (read risk); an ask rule forces approval.
    prefs = {}
    store = RuleStore(prefs, lambda: None)
    store.add("read_file", ASK)
    eng = PermissionEngine(workspace_root=tmp_path, rule_resolver=store.resolver())
    d = eng.evaluate("read_file", {}, None)
    assert d.allowed is False
    assert d.needs_user is True


def test_no_matching_rule_defers_to_risk(tmp_path):
    # With no matching rule, the engine falls back to risk classification — same as
    # having no rule_resolver at all.
    prefs = {}
    store = RuleStore(prefs, lambda: None)
    store.add("mcp__notion__get_page", ALLOW)
    eng = PermissionEngine(workspace_root=tmp_path, rule_resolver=store.resolver())
    # A different tool (no rule) still gates as external-risk.
    d = eng.evaluate("mcp__notion__delete_page", {}, MCP_META)
    assert d.allowed is False
    assert d.needs_user is True


def test_rule_resolver_none_is_default_behavior(tmp_path):
    # No rule_resolver → the engine behaves exactly as before E2.5.
    eng = PermissionEngine(workspace_root=tmp_path)
    # read tool auto-allows.
    assert eng.evaluate("read_file", {}, None).allowed is True
    # external-risk MCP tool gates.
    d = eng.evaluate("mcp__notion__get_page", {}, MCP_META)
    assert d.allowed is False and d.needs_user is True


def test_rule_allow_wins_over_auto_mode(tmp_path):
    # Even in AUTO mode (which allows everything), an explicit deny rule still blocks —
    # the user's explicit permission statement overrides the mode.
    prefs = {}
    store = RuleStore(prefs, lambda: None)
    store.add("mcp__dangerous__*", DENY)
    eng = PermissionEngine(
        workspace_root=tmp_path, mode=Mode.AUTO, rule_resolver=store.resolver()
    )
    d = eng.evaluate("mcp__dangerous__nuke", {}, MCP_META)
    assert d.allowed is False
    assert d.needs_user is False

