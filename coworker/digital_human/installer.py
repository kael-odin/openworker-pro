"""DHP → openworker installer.

Turns a parsed :class:`~coworker.digital_human.spec.DigitalHumanSpec` into a live
:class:`~coworker.automation.models.ScheduledTask` and records an
:class:`~coworker.digital_human.instances.DigitalHumanInstance` linking the two.

The mapping is the heart of the DHP bridge — DHP's declarative spec is a *recipe* for an openworker
automation, and this module is the *cook*:

* ``system_prompt`` → ``instructions``, with a ``## 用户配置`` preamble prepended carrying the
  resolved user config (so the agent reads its parameters from the prompt, exactly as DHP specs
  expect — most prompts already say "从 User Configuration 读取").
* ``subscriptions[0]`` schedule → ``Schedule.cron`` (already resolved by the parser). A spec with no
  schedule produces a manual-run task (``fire_at`` far future, ``enabled=False``) — installable but
  only runs on demand.
* ``output.notify.channels`` → ``notify_channels``; if the spec declares channels, the level defaults
  to ``all`` (the author opted into push); otherwise ``important``.
* secret-marked config fields → SecretStore; the rest → the instance record.

Installing does **not** run the task — it creates it (disabled if manual-run) and returns the
instance + task so the GUI can open it. The user (or a schedule) starts runs.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from ..automation.models import ScheduledTask, Schedule
from .instances import DigitalHumanInstance, InstanceStore, SECRET_PROFILE_PREFIX
from .spec import DigitalHumanSpec, SpecError

# A manual-run automation needs *some* schedule to satisfy the ScheduledTask contract. Use a cron
# that never fires (February 30th is impossible) and keep the task disabled — runs are manual only.
_NEVER_CRON = "0 0 30 2 *"


_SECRET_MARKER = "<configured>"  # typed marker for a secret value held in SecretStore only


def _preamble(non_secret: dict[str, Any], secret_keys: list[str]) -> str:
    """Build the ``## 用户配置`` preamble from non-secret values + typed secret markers.

    Secret values are NEVER serialized into the instructions: each secret key is rendered as
    ``<configured>`` so the model sees the key is set, but the value lives only in the
    SecretStore profile (``SECRET_PROFILE_PREFIX<id>``) and never enters the prompt, the task
    API response, or the audit log. The runtime resolves a secret at the tool/capability
    boundary, not from the static instructions."""
    safe_config: dict[str, Any] = dict(non_secret)
    for key in secret_keys:
        safe_config[key] = _SECRET_MARKER
    preamble_lines = [
        "## 用户配置 (userConfig)",
        "以下是本次运行的配置参数，请按此配置执行：",
        "```json",
        json.dumps(safe_config, ensure_ascii=False, indent=2),
        "```",
    ]
    return "\n".join(preamble_lines)


def build_instructions(
    spec: DigitalHumanSpec,
    config: dict[str, Any],
    *,
    secret_keys: Optional[list[str]] = None,
) -> str:
    """Prepend a ``## 用户配置`` preamble (the resolved userConfig JSON) to the system prompt.

    DHP prompts reference ``userConfig[<key>]``; openworker has no separate config channel, so the
    values are injected into the prompt itself. Missing required keys are caught earlier by
    :func:`validate_config`; here we trust the config is complete.

    ``secret_keys`` names config fields whose values must NOT appear in the instructions. For
    each such key the preamble carries a ``<configured>`` marker; the real value stays in the
    SecretStore and is resolved at the capability boundary at run time. Passing the secret
    values in ``config`` is a caller bug — this function drops them defensively, but callers
    should pass non-secret config only."""
    non_secret, _secret, declared_secret_keys = split_config(spec, config)
    # Merge any caller-declared secret keys (the install/edit path already split them out
    # before calling here, so config carries only non-secret values; declared_secret_keys is
    # empty in that path). Union so a key flagged secret on either side is masked.
    all_secret_keys = sorted(set((secret_keys or []) + declared_secret_keys))
    # Defensive: strip any secret value that slipped through in `config` despite the caller
    # having split it — a secret value in the instructions is the exact leak we prevent.
    return _preamble(non_secret, all_secret_keys) + "\n\n" + spec.system_prompt


def reinstall_instructions(
    spec: DigitalHumanSpec,
    config: dict[str, Any],
    *,
    system_prompt: Optional[str] = None,
    secret_keys: Optional[list[str]] = None,
) -> str:
    """Rebuild task instructions for an already-installed instance (the edit path).

    Mirrors :func:`build_instructions`, but lets the caller override the spec's system prompt with
    a user-edited value (from the Developer block). When ``system_prompt`` is None, the spec's own
    prompt is used unchanged — the same as a fresh install. Secret values are masked the same way
    as in :func:`build_instructions`; pass ``secret_keys`` to declare them."""
    non_secret, _secret, declared_secret_keys = split_config(spec, config)
    all_secret_keys = sorted(set((secret_keys or []) + declared_secret_keys))
    body = system_prompt if system_prompt is not None else spec.system_prompt
    return _preamble(non_secret, all_secret_keys) + "\n\n" + (body or "")


def validate_config(spec: DigitalHumanSpec, config: dict[str, Any]) -> list[str]:
    """Return a list of missing-required config keys (empty = valid). Also coerces types where
    possible (number/boolean) and fills defaults — mutates ``config`` in place."""
    errors: list[str] = []
    for f in spec.config_schema:
        val = config.get(f.key)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            if f.default is not None:
                config[f.key] = f.default
                continue
            if f.required:
                errors.append(f.key)
            continue
        # Coerce: DHP config values often arrive as strings from a form.
        if f.type == "number":
            try:
                config[f.key] = int(val) if str(val).isdigit() else float(val)
            except (TypeError, ValueError):
                errors.append(f.key)
        elif f.type == "boolean":
            if isinstance(val, str):
                config[f.key] = val.strip().lower() in ("1", "true", "yes", "on")
    return errors


def split_config(
    spec: DigitalHumanSpec, config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Split resolved config into (non_secret, secret, secret_keys) per the field heuristic."""
    non_secret: dict[str, Any] = {}
    secret: dict[str, Any] = {}
    secret_keys: list[str] = []
    secret_keyset = {f.key for f in spec.config_schema if f.is_secret}
    for k, v in config.items():
        if k in secret_keyset:
            secret[k] = v
            secret_keys.append(k)
        else:
            non_secret[k] = v
    return non_secret, secret, secret_keys


def install_digital_human(
    spec: DigitalHumanSpec,
    config: dict[str, Any],
    *,
    task_store,
    scratch_provider,
    instances: InstanceStore,
) -> dict[str, Any]:
    """Install a parsed spec as an openworker automation.

    Args:
        spec: the parsed DHP spec.
        config: the user's config values (will be validated + defaulted in place).
        task_store: a :class:`~coworker.automation.store.TaskStore` (``.save(task)``).
        scratch_provider: callable ``(task_session_id) -> workspace_path`` (manager._provision_scratch).
        instances: the :class:`InstanceStore` to record the link in.

    Returns ``{"ok": True, "instance": ..., "task": ...}`` or ``{"ok": False, "error": ...}``.
    """
    missing = validate_config(spec, config)
    if missing:
        return {"ok": False, "error": f"missing required config: {', '.join(missing)}"}

    non_secret, secret, secret_keys = split_config(spec, config)

    # Resolve the schedule. A spec with no schedule (manual-run) gets a never-firing cron + disabled.
    sub = spec.primary_schedule
    if sub is not None and sub.cron:
        cron = sub.cron
        enabled = True
    else:
        cron = _NEVER_CRON
        enabled = False

    # Secret config goes to SecretStore before the task is created, so a run can resolve it.
    instance_id = "dh-" + uuid.uuid4().hex[:10]
    if secret_keys and instances.secrets is not None:
        instances.secrets.put(f"{SECRET_PROFILE_PREFIX}{instance_id}", secret)

    # The merged config (non-secret + secret placeholders) for the prompt. Secret values live
    # only in the SecretStore profile; the instructions carry a typed ``<configured>`` marker for
    # each secret key so the model sees the key is set without ever receiving the value.
    instructions = build_instructions(spec, non_secret, secret_keys=secret_keys)

    task = ScheduledTask(
        title=spec.name,
        instructions=instructions,
        schedule=Schedule(kind="cron", cron=cron),
        workspace="",  # set below via scratch_provider
        origin_surface="digital-human",
        agent="cowork",
        notify_channels=list(spec.notify_channels),
        notify_level="all" if spec.notify_channels else "important",
        enabled=enabled,
    )
    task.workspace = scratch_provider(task.task_session_id)
    task_store.save(task)

    inst = DigitalHumanInstance(
        id=instance_id,
        slug=spec.slug,
        name=spec.name,
        task_id=task.id,
        config=non_secret,
        secret_keys=secret_keys,
        spec_version=spec.version,
        # Fresh installs never leak: instructions carry typed ``<configured>`` markers, not values.
        needs_secret_migration=False,
        unreviewed=False,
    )
    instances.put(inst)

    return {"ok": True, "instance": inst.to_dict(), "task": task.public()}


def uninstall_digital_human(
    instance_id: str,
    *,
    instances: InstanceStore,
    task_store,
) -> dict[str, Any]:
    """Remove an installed digital human: delete the linked task + the instance record (and its
    secret profile, handled by InstanceStore.delete)."""
    inst = instances.get(instance_id)
    if inst is None:
        return {"ok": False, "error": "instance not found"}
    task_store.delete(inst.task_id)
    instances.delete(instance_id)
    return {"ok": True, "id": instance_id}
