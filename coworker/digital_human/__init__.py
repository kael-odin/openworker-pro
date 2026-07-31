"""DHP (Digital Human Protocol) digital-human packages for openworker.

A DHP package is a ``spec.yaml`` (see the DHP ``spec/app-spec.md``) that declares an autonomous
agent — its system prompt, trigger subscriptions, user config schema, dependencies, and output
channels. This package **bridges** DHP into openworker's existing systems rather than running a
parallel runtime:

* ``spec.py``      — pure parser: ``spec.yaml`` text → :class:`DigitalHumanSpec`. No I/O.
* ``store.py``     — :class:`DhpRegistry`: loads ``index.json`` + resolves specs from a local
                     DHP repo clone (or the official GitHub Pages index).
* ``installer.py`` — :func:`install_digital_human`: spec → :class:`~coworker.automation.models.ScheduledTask`
                     (system_prompt → instructions, cron from subscriptions, notify channels wired,
                     user config injected into the prompt preamble).
* ``instances.py`` — :class:`InstanceStore`: which digital humans are installed, their non-secret
                     config, and the linked ``task_id``. Secret-marked config fields go to SecretStore.

Mapping (DHP → openworker)::

    system_prompt                         → ScheduledTask.instructions (+ config preamble)
    subscriptions[].source.config.cron    → Schedule.cron
    subscriptions[].source.config.every   → Schedule.cron (interval → cron expression)
    output.notify.channels                → ScheduledTask.notify_channels
    config_schema + user values           → injected as ``## 用户配置`` JSON in the prompt
    requires.mcps / permissions           → consent summary (display); ``ai-browser`` is a known cap
    name / description / icon / store.*   → registry listing metadata + task title
"""

from __future__ import annotations

from .spec import (
    DigitalHumanSpec,
    SpecError,
    parse_spec,
    load_spec_file,
    ConfigField,
    SubscriptionDef,
    McpDependency,
    SkillDependency,
)
from .store import DhpRegistry, RegistryEntry
from .instances import DigitalHumanInstance, InstanceStore
from .installer import install_digital_human, uninstall_digital_human

__all__ = [
    "DigitalHumanSpec",
    "SpecError",
    "parse_spec",
    "load_spec_file",
    "ConfigField",
    "SubscriptionDef",
    "McpDependency",
    "SkillDependency",
    "DhpRegistry",
    "RegistryEntry",
    "DigitalHumanInstance",
    "InstanceStore",
    "install_digital_human",
    "uninstall_digital_human",
]
