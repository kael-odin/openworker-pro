"""Session manager — owns engines (one per session), stores, and the provider.

Each session is bound to a workspace folder (Code requires one). Storage is a single DB
under a data dir (global for the real server, per-workspace for tests), so recents and
sessions span folders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from ..agent import build_engine
from ..agents import get_agent
from ..connections import (
    PersonaConnectionStore,
    SessionConnectionStore,
    effective as effective_connections,
)
from ..inbox import InboxStore, args_preview
from ..inbox_routing import InboxRouting
from ..personas import PersonaRegistry
from ..personas.registry import set_registry as set_persona_registry
from ..selfwake import WakeStore
from ..mentions import MentionSessionStore
from ..subscriptions import ChannelBuffer, SubscriptionStore
from ..unrouted import UnroutedStore
from ..unattended import UnattendedRegistry
from ..audit import AuditStore
from ..config import load_config, workspace_allowed_commands
from ..conversations import ConversationStore, title_from
from ..engine import ApprovalOutcome, Approver, TurnEngine
from ..roots import RootDir
from ..workspace_trust import WorkspaceTrustStore
from ..automation import Schedule, ScheduledTask, Scheduler, TaskRun, TaskStore
from ..connectors import (
    Gateway,
    MessageSource,
    SendResult,
    connect_connector,
    connector_list,
    disconnect_connector,
    experimental_enabled,
    load_settings,
    make_adapter,
    set_experimental_enabled,
    slack_split,
    update_connector_tools,
)
from ..connectors.browser_automation import (
    browser_close_session,
    browser_state,
    browser_take_screenshot,
)
from ..connectors.parked import ParkedStore
from ..mcp import (
    MCPManager,
    build_callables,
    delete_global_server,
    load_mcp_servers,
    patch_global_server,
    put_global_server,
    read_global,
)
from ..memory import MemoryStore, Scope, SQLiteMemoryStore
from ..permissions import Mode
from ..agents import list_agents as _list_agents
from ..providers import (
    ProviderClient,
    ProviderRouter,
    descriptor_configured,
    get_descriptor,
    provider_descriptors,
    verify_provider_key,
)
from ..secrets import SecretStore, state_dir
from ..sessions import SessionRecord
from ..skills import (
    SessionSkillStore,
    SkillLoader,
    SkillStore,
    effective_skills,
)

_SCOPES = {s.value for s in Scope}

logger = logging.getLogger("coworker.manager")


def _grants_of(engine) -> dict[str, Any]:
    """The engine's session-scoped "Always allow" approvals, in persistable shape."""
    tools = sorted(getattr(engine.permissions, "session_allow_tools", None) or ())
    commands = sorted(getattr(engine.permissions, "session_allow_commands", None) or ())
    return {"tools": tools, "commands": commands} if (tools or commands) else {}


def _parse_allowed_tools(value: Any) -> Optional[list[str]]:
    """Normalize a request-body allowed_tools value into a clean list (or None to leave untouched).

    Accepts a comma-separated string ("read_file, write_file"), a list of strings, or None.
    Used by create_skill/update_skill so the frontmatter `allowed-tools` line is writable from
    the Settings form. Returns None when the key is absent so update() can preserve the existing
    list; returns [] for an empty string/list so the field can be cleared.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, (list, tuple)):
        return [str(t).strip() for t in value if str(t).strip()]
    return None


def _approval_body(request) -> str:
    """Approval card body: the tool's reason (if any) plus a compact preview of its args, so a
    mirrored 'Run `write_file`?' shows the path/content rather than just the tool name.
    """
    reason = (getattr(request, "reason", "") or "").strip()
    preview = args_preview(getattr(request, "arguments", None))
    return "\n".join(p for p in (reason, preview) if p)


class SessionManager:
    def __init__(
        self,
        *,
        workspace: Optional[str | Path] = None,  # default/seed workspace (e.g. --cwd)
        data_dir: Optional[str | Path] = None,
        model: str = "gpt-5.6-sol",
        mode: Mode = Mode.INTERACTIVE,
        provider: Optional[ProviderClient] = None,
    ) -> None:
        self.default_workspace = (
            str(Path(workspace).expanduser().resolve()) if workspace else None
        )
        self.model = model
        self.mode = mode
        self.provider = provider

        if data_dir is not None:
            base = Path(data_dir).expanduser()
        elif self.default_workspace is not None:
            base = Path(self.default_workspace) / ".coworker"
        else:
            base = state_dir()
        base.mkdir(parents=True, exist_ok=True)

        self.memory_store: MemoryStore = SQLiteMemoryStore(base / "coworker.db")
        self.audit_store = AuditStore(base / "coworker.db")
        self.session_store = ConversationStore(base)
        self.session_store.canonicalize_workspaces()  # collapse /tmp vs /private/tmp etc.
        if self.default_workspace:
            self.session_store.touch_workspace(self.default_workspace)
        self._engines: dict[str, TurnEngine] = {}
        self._running_sessions: set[str] = (
            set()
        )  # sessions with an in-flight turn (busy)
        # Sessions with an auto-title LLM call in flight (FB-010) — one call at a time.
        self._autotitle_inflight: set[str] = set()
        self._autotitle_tasks: set[asyncio.Task] = set()
        self._autotitle_attempts: dict[str, int] = {}
        self.workspace_trust = WorkspaceTrustStore()
        self.secrets = SecretStore()
        # Personal-WeChat QR attempts own short-lived polling clients and never
        # expose the polling transaction or confirmed credentials. A successful
        # scan hot-reloads the one shared messaging gateway.
        from ..connectors.wechat_ilink.auth import QrAttemptRegistry

        self.wechat_ilink_qr = QrAttemptRegistry(
            self.secrets, on_confirm=self._wechat_ilink_confirmed
        )
        # No explicit provider injected → route by the model's `provider:` prefix (OpenAI default,
        # Ollama, …). Tests inject a provider directly and bypass the router. The same router is
        # shared by every engine and the `/v1/chat/completions` proxy.
        if self.provider is None:
            self.provider = ProviderRouter(
                self.secrets, default_provider="openai", on_use=self._note_provider_use
            )
        self.mcp = MCPManager(secrets=self.secrets)
        # OAuth MCP servers with a sign-in in flight / their last connect error —
        # feeds list_mcp's status so the GUI can show "authorizing…" and failures.
        self._mcp_authorizing: set[str] = set()
        self._mcp_errors: dict[str, str] = {}
        self.gateway: Optional[Gateway] = None
        self._gateway_loop: Optional[asyncio.AbstractEventLoop] = None
        self._data_base = base
        # Desktop/UI prefs (default model, onboarding state) — not secrets; a plain JSON file.
        self._prefs = self._load_prefs()
        if self._prefs.get("default_model"):
            self.model = self._prefs["default_model"]
        # Seed the PDF-fallback module global from prefs so engines see the user's
        # choice from the first turn (set_pdf_settings keeps it in sync after).
        from ..pdf_support import set_fallback_mode

        set_fallback_mode(self.pdf_settings()["pdf_fallback"])
        # Per-session live-view registry: every socket open on a session id gets the turn's events,
        # whoever drives the turn (foreground user_message, channel delivery, self-wake, resume).
        # Delivery itself is socket-independent — this only governs *live visibility*.
        self._session_clients: dict[str, set[Any]] = {}
        # App-wide event sockets (/ws/events): session-independent pushes — today the
        # automation-run-started toast (UX-026); badges could ride it later.
        self._event_clients: set[Any] = set()
        # Automation: scheduled tasks store + the tick scheduler (started in the lifespan).
        # The scheduler also resumes self-wake'd sessions each tick (extra_tick).
        self.task_store = TaskStore(base / "automation.db")
        # 多渠道通知（钉钉/飞书/企微/webhook/邮件）—— 配置存 secrets，调度完成时分发。
        from ..notify import NotifyRouter, NotifyConfigStore

        self.notify_router = NotifyRouter(NotifyConfigStore(self.secrets))
        self.scheduler = Scheduler(
            self.task_store, self._run_scheduled_task, extra_tick=self.resume_due_wakes
        )
        # Personas: registry + lifecycle state under this manager's data dir. Installed as the
        # process singleton so agents.get_agent resolves persona ids (incl. third-party) here.
        self.personas = PersonaRegistry(state_path=base / "personas.json")
        set_persona_registry(self.personas)
        # Persona marketplace sources (E4 后续) — git repos of *.md persona manifests the user
        # can browse + install from. Mirrors plugin_sources / skill_sources. No builtin source
        # today (no Anthropic-official persona marketplace yet); the user adds their own. The
        # empty-store guard (builtins can't be deleted) still applies if we ship one later.
        from ..personas import PersonaSourceManager

        self.persona_sources = PersonaSourceManager(self._prefs, self._save_prefs)
        self.persona_sources.ensure_builtins()
        self._persona_cache_root = base / "persona_sources_cache"
        # DHP digital-human registry + installed instances (批次 B, 多源化于批次 D2). Sources are
        # persisted in prefs (dhp_sources) and the default HTTP source is re-asserted on startup, so
        # the store is never empty even without a local clone or OPENWORKER_DHP_REPO env — the root
        # cause of the empty-store bug.
        from ..digital_human import DhpRegistry, InstanceStore, SourceManager

        self.dhp_sources = SourceManager(self._prefs, self._save_prefs)
        self.dhp_sources.ensure_builtins()
        # If a local DHP repo is configured via env, register it as an additional local source so
        # dev/test still benefits from the clone without losing the default HTTP source.
        import os as _os

        _env_repo = _os.environ.get("OPENWORKER_DHP_REPO")
        if _env_repo and Path(_env_repo).is_dir():
            existing = {s.url for s in self.dhp_sources.list()}
            if _env_repo not in existing:
                self.dhp_sources.add("本地 DHP 仓库", _env_repo, source_type="local")
        self.dhp_registry = DhpRegistry(self.dhp_sources.list(enabled_only=True))
        self.dhp_instances = InstanceStore(base / "digital-humans.json", secrets=self.secrets)
        # Skills (SKILLS-SPEC §4): folder-backed CRUD + per-session mutes. The effective menu
        # gates the engine's skill catalog the same way effective_connectors gates connector
        # tools — one resolver feeds the catalog injection, the rail, and the composer popup.
        # Complementary to the E1-E5 marketplace install layer below (skill_sources): both
        # land in state_dir()/skills/<name>/ where SkillLoader discovers them.
        self.skill_store = SkillStore()
        self.session_skills = SessionSkillStore(base / "session_skills.json")
        # Skill sources (批次 E1) — same prefs-persisted SourceManager pattern as DHP. Skills
        # install into state_dir()/skills/<name>/ (the SkillLoader discovery dir); git-source
        # clones are cached under state_dir()/skill_sources_cache/ and shared across installs.
        from ..skills import SkillSourceManager

        self.skill_sources = SkillSourceManager(self._prefs, self._save_prefs)
        self.skill_sources.ensure_builtins()
        self._skill_cache_root = base / "skill_sources_cache"
        # Rules (allow/deny/ask permission layer, E2) + Hooks (pre_run/post_run, E2) —
        # same prefs-persisted store pattern as skill_sources. Rules are the user-facing
        # permission layer above overrides.py's risk-class relaxer; hooks fire around
        # scheduled runs (pre_run before engine build, post_run in the finally block).
        from ..rules import RuleStore
        from ..hooks import HookStore

        self.rule_store = RuleStore(self._prefs, self._save_prefs)
        self.hooks = HookStore(self._prefs, self._save_prefs)
        # Commands (E3) — reusable slash command templates (`/name`). Read-only discovery of
        # state_dir()/commands/<name>/COMMAND.md; command files are hand-authored or installed
        # via E4 plugin packaging. Same loader pattern as SkillLoader, but commands are
        # user-triggered prompt templates, not agent tools.
        from ..commands import CommandLoader

        self.command_loader = CommandLoader([state_dir() / "commands"])
        # Plugins (E4) — Claude-Code-format distribution units installed from marketplace
        # sources. Plugins live under state_dir()/plugins/<name>/; their skills/ and commands/
        # subfolders are picked up by the loaders above (see _plugin_skill_dirs /
        # _plugin_command_dirs). The official Anthropic marketplace is built in.
        from ..plugins import PluginSourceManager, PluginRegistry

        self.plugin_sources = PluginSourceManager(self._prefs, self._save_prefs)
        self.plugin_sources.ensure_builtins()
        self._plugin_cache_root = base / "plugin_sources_cache"
        self.plugin_registry = PluginRegistry(self._prefs, self._save_prefs)
        # Browser login state (E5) — persisted login sessions (Playwright storageState or
        # pasted cookies) under state_dir()/browser_profiles/<id>/. When the agent opens a
        # browser URL whose host matches a captured login, browser_automation rebuilds
        # the context with that storageState so the agent is already authenticated.
        from ..browser_logins import BrowserLoginRegistry

        self.browser_logins = BrowserLoginRegistry(self._prefs, self._save_prefs)
        self._browser_profiles_dir = base / "browser_profiles"
        self._browser_profiles_dir.mkdir(parents=True, exist_ok=True)
        # Wire the decoupled resolver used by browser_open_url.
        from ..connectors.browser_automation import set_login_state_resolver

        set_login_state_resolver(self._resolve_login_state_for_url)
        # Inbox (cross-session human-attention queue), routing (named inboxes + Slack/Telegram
        # bindings), the Unattended toggle, and self-wake records.
        self.inbox = InboxStore(base / "inbox.json")
        self.inbox_routing = InboxRouting(base / "inbox_routing.json")
        self.unattended = UnattendedRegistry(base / "unattended.json")
        self.wakes = WakeStore(base / "wakes.json")
        # Channel subscriptions (inbound): persisted (session_id, channel) records + a ring buffer
        # of recently-seen channel messages for get_channel_messages.
        self.subscriptions = SubscriptionStore(base / "subscriptions.json")
        self.channel_buffer = ChannelBuffer(state_path=base / "channels.json")
        # Mention router (§31): thread target → the session that owns that Slack thread.
        # Also the durable source of the thread's standing send_message grant (re-seeded
        # onto the engine in get_engine).
        self.mention_sessions = MentionSessionStore(base / "mention_threads.json")
        # Unauthorized inbound messages, parked instead of dropped (one-step allow-and-deliver).
        self.parked = ParkedStore(base / "parked.json")
        # People directory: "platform:user_id" → display name, noted from every inbound
        # (authorized or parked) so allow-list chips read "Rohit Prsad", not "U07JK…".
        self._people_path = base / "people.json"
        try:
            self._people: dict[str, str] = json.loads(self._people_path.read_text())
        except (OSError, ValueError):
            self._people = {}
        # Seed from already-parked messages (they carry resolved names) so an allow made from
        # an old parked item still gets a named chip.
        for it in self.parked.list():
            if it.get("user_name"):
                self._people.setdefault(
                    f"{it['platform']}:{it['user_id']}", it["user_name"]
                )
        # Connection hierarchy (UI-REFRESH §4): per-persona default connector on/off (seeded from the
        # manifest, then user-editable) + per-session overrides. Resolved into the session's effective
        # connector set, which gates inbound delivery and the engine's connector tools.
        self.persona_connections = PersonaConnectionStore(
            base / "persona_connections.json"
        )
        self.session_connections = SessionConnectionStore(
            base / "session_connections.json"
        )
        # Dead-letter: inbound messages with no destination + background-turn failures, so neither
        # vanishes silently (a debugging/visibility surface, not a redelivery queue).
        self.unrouted = UnroutedStore(base / "unrouted.json")

    # -- workspaces -------------------------------------------------------------
    def open_workspace(self, path: str, *, create: bool = False) -> dict[str, Any]:
        resolved = Path(path).expanduser()
        if resolved.exists() and not resolved.is_dir():
            return {"path": str(resolved), "ok": False, "error": "not a directory"}
        if not resolved.exists():
            if not create:
                return {
                    "path": str(resolved),
                    "ok": False,
                    "error": "folder does not exist",
                }
            try:
                resolved.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return {"path": str(resolved), "ok": False, "error": str(exc)}
        resolved = resolved.resolve()
        self.session_store.touch_workspace(str(resolved))
        return {
            "path": str(resolved),
            "ok": True,
            "git_branch": _git_branch(resolved),
            "command_trust": self.workspace_command_trust(resolved),
        }

    def workspace_command_trust(self, path: str | Path) -> dict[str, Any]:
        if not str(path).strip():
            return {
                "workspace": "",
                "requested_commands": [],
                "trusted": False,
                "required": False,
            }
        canonical = WorkspaceTrustStore.canonical(path)
        commands = (
            workspace_allowed_commands(canonical)
            if Path(canonical).is_dir()
            else []
        )
        trusted = self.workspace_trust.is_trusted(canonical)
        return {
            "workspace": canonical,
            "requested_commands": commands,
            "trusted": trusted,
            "required": bool(commands and not trusted),
        }

    def _mcp_workspace_trusted(self, workspace: Optional[str | Path]) -> bool:
        """Whether workspace `.coworker/mcp.json` may be loaded (#213).

        Same consent boundary as repository ``allowed_commands``: an untrusted
        clone must not define stdio processes that spawn at session open.
        """
        return bool(workspace and self.workspace_trust.is_trusted(workspace))

    def set_workspace_trust(
        self, path: str | Path, *, trusted: bool
    ) -> dict[str, Any]:
        if not str(path).strip():
            return {"ok": False, "error": "workspace path is required"}
        candidate = Path(path).expanduser()
        if trusted and not candidate.is_dir():
            return {"ok": False, "error": "workspace is not a directory"}
        canonical = self.workspace_trust.set_trusted(candidate, trusted)
        effective = load_config(
            canonical, workspace_trusted=trusted
        ).allowed_commands
        # Apply trust/revocation immediately to live sessions rooted at this exact path.
        for engine in self._engines.values():
            engine_workspace = str(
                (getattr(engine, "audit_context", {}) or {}).get("workspace", "")
            )
            if engine_workspace and WorkspaceTrustStore.canonical(
                engine_workspace
            ) == canonical:
                engine.permissions.allowed_commands = list(effective)
        return {
            "ok": True,
            **self.workspace_command_trust(canonical),
        }

    def trusted_workspaces(self) -> list[dict[str, Any]]:
        return [
            {
                **self.workspace_command_trust(path),
                "exists": Path(path).is_dir(),
            }
            for path in self.workspace_trust.list()
        ]

    def recent_workspaces(self) -> list[dict[str, Any]]:
        """Recent real projects for the folder gate. Per-conversation scratch dirs are
        excluded — they're workspaces to the session store, but never something a user
        should re-open as a 'project'."""
        scratch = self.scratch_base().resolve()
        out = []
        for path in self.session_store.recent_workspaces():
            p = Path(path)
            try:
                if p.resolve().is_relative_to(scratch):
                    continue
            except OSError:
                pass
            out.append({"path": path, "name": p.name, "exists": p.is_dir()})
        return out

    DEFAULT_SCRATCH_BASE = "~/OpenWorker"

    def scratch_base(self) -> Path:
        """Common area for per-conversation scratch directories. Configurable via prefs."""
        base = self._prefs.get("scratch_base") or self.DEFAULT_SCRATCH_BASE
        return Path(base).expanduser()

    def _provision_scratch(self, session_id: str) -> str:
        """Create (idempotently) and return this conversation's scratch directory."""
        d = self.scratch_base() / session_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d.resolve())

    def resolve_workspace(self, requested: Optional[str]) -> Optional[str]:
        if requested:
            p = Path(requested).expanduser()
            if p.is_dir():
                return str(p.resolve())
            return None
        return self.default_workspace

    # -- engines ----------------------------------------------------------------
    def engine_workspace(
        self, session_id: str, *, workspace: Optional[str] = None, agent: str = "code"
    ) -> Optional[str]:
        """The workspace `get_engine` would bind — for prepping MCP tools beforehand."""
        record = self.session_store.load(session_id)
        if record:
            return record.workspace or None
        ag = get_agent(agent or "code")
        return self.resolve_workspace(workspace) if ag.needs_workspace else None

    def get_engine(
        self,
        session_id: str,
        *,
        workspace: Optional[str] = None,
        agent: str = "code",
        approver: Optional[Approver] = None,
        extra_tools: Optional[list[Any]] = None,
        directory_requester: Optional[Any] = None,
        plan_approver: Optional[Any] = None,
        question_asker: Optional[Any] = None,
    ) -> Optional[TurnEngine]:
        engine = self._engines.get(session_id)
        if engine is not None:
            if approver is not None:
                engine.approver = approver
            if directory_requester is not None:
                engine.directory_requester = directory_requester
            if plan_approver is not None:
                engine.plan_approver = plan_approver
            if question_asker is not None:
                engine.question_asker = question_asker
            return engine

        record = self.session_store.load(session_id)
        is_new_session = record is None
        agent_name = (record.agent if record else agent) or "code"
        ag = get_agent(agent_name)

        if record:
            ws = record.workspace or None
            model, mode, messages = record.model, Mode(record.mode), record.messages
        else:
            ws = self.resolve_workspace(workspace) if ag.needs_workspace else None
            model, mode, messages = self.model, self.mode, None

        if ag.needs_workspace and (not ws or not Path(ws).is_dir()):
            # Knowledge surfaces (Cowork, Ops, …) start "orphan": no folder picked →
            # auto-provision a per-conversation scratch directory (generalizes MyHelper's
            # auto-workspace). Code-family surfaces still require a real repo; Chat needs none.
            if ag.family == "knowledge":
                ws = self._provision_scratch(session_id)
            else:
                return None

        if ws:
            self.session_store.touch_workspace(ws)
        # Orphan surfaces are multi-root: the scratch (ws) is the primary writable root, plus any
        # folders the user added (persisted per session). Code/Chat stay single-root (roots=None).
        roots = None
        if ag.family == "knowledge" and ws:
            extra = [
                r
                for r in ((record.extra_roots if record else []) or [])
                if Path(str(r.get("path", ""))).is_dir()
            ]
            roots = [{"path": ws, "writable": True, "label": "scratch"}, *extra]
        engine = build_engine(
            agent=ag,
            workspace=ws,
            model=model,
            mode=mode,
            provider=self.provider,
            memory_store=self.memory_store,
            messages=messages,
            extra_tools=extra_tools,
            secrets=self.secrets,
            task_store=self.task_store,
            wake_store=self.wakes,
            session_id=session_id,
            audit_sink=self.audit_store.append,
            roots=roots,
            # WS sessions pass mode-aware callbacks (attended → live prompt, unattended → Inbox).
            # Background / self-wake / durable-resume runs have no live socket → default to the
            # Inbox-based callbacks so a rebuilt engine can still get approvals/answers (and, on
            # resume, the already-resolved item returns immediately).
            approver=approver or self.inbox_approver(session_id, agent),
            directory_requester=directory_requester
            or self.inbox_directory_requester(session_id, agent),
            plan_approver=plan_approver or self.inbox_plan_approver(session_id, agent),
            question_asker=question_asker
            or self.inbox_question_asker(session_id, agent),
            subscription_store=self.subscriptions,
            channel_buffer=self.channel_buffer,
            routing_targets=self._routing_targets(session_id, agent),
            # Per-session connection hierarchy: expose only effective-enabled connectors' tools.
            connector_filter=self.effective_connectors(session_id, agent_name),
            # Per-session skill menu, LIVE (SKILLS-SPEC §3): a callable so load_skill sees
            # disables/new skills immediately; the catalog snapshot is taken at build.
            skill_filter=lambda sid=session_id, w=ws: self.effective_skill_names(sid, w),
            # User-facing permission rules (E2): allow/deny/ask takes precedence over
            # risk classification in the permission engine.
            rule_resolver=self.rule_store.resolver(),
            # Persona delegation (E3) + slash commands (E3): let code-family agents
            # delegate subtasks to any installed persona, and surface available /commands.
            persona_registry=self.personas,
            command_loader=self._engine_command_loader(),
            live_delivery=self._live_delivery,
            # Tool-level hooks (pre_tool/post_tool/on_message) — the same HookStore that
            # fires pre_run/post_run. Firer is None when no tool-event hooks are
            # registered, so the hot path skips the subprocess call entirely.
            tool_hook_firer=self._tool_hook_firer(),
        )
        # An automation run rebuilt here (manual "Run now" over WS, durable resume) still
        # carries its task's standing allowances — the rules live on the task record.
        owning_task = self.task_store.task_for_run_session(session_id)
        if owning_task is not None:
            self._seed_task_permissions(engine, owning_task)
        # A mention-spawned session (§31) keeps its in-thread reply pre-approved across
        # rebuilds/restarts — the grant is re-derived from the durable thread map.
        for thread_target in self.mention_sessions.targets_for(session_id):
            engine.permissions.task_rules.setdefault("send_message", set()).add(
                thread_target
            )
        if record is not None and record.grants:
            self._apply_grants(engine, record.grants)
        # Auto-compaction (OPE-27): restore the persisted view boundary and wire the live
        # Settings getter — post-construction, so build_engine's signature stays put.
        if record is not None and record.compaction:
            from ..compaction import CompactionState

            engine.compaction_state = CompactionState.from_dict(record.compaction)
        engine.compaction_settings = self.compaction_settings
        self._engines[session_id] = engine
        if is_new_session:
            self._emit_session_created(session_id, agent_name)
        return engine

    def _emit_session_created(self, session_id: str, persona_id: str) -> None:
        """Phase 5 telemetry, fired once per brand-new session on a background thread
        (never blocks session start). cloud.emit_session_created is a hard no-op when
        signed out or opted out, and sends only content-free facts."""
        import threading

        from .. import cloud
        from ..config import load_config

        entry = self.personas.get(persona_id)
        family = entry.family if entry else ""
        workspace_kind = entry.workspace if entry else ""

        def _send() -> None:
            try:
                cloud.emit_session_created(
                    self.secrets,
                    load_config(),
                    session_id=session_id,
                    persona_id=persona_id,
                    persona_family=family,
                    workspace_kind=workspace_kind,
                )
            except Exception:
                pass  # telemetry must never surface as a session error

        threading.Thread(target=_send, daemon=True).start()

    def _routing_targets(self, session_id: str, agent: str) -> list[str]:
        """The channel address(es) this session's Inbox routes OUT to — used to warn when a
        subscription (inbound) collides with Inbox routing (outbound) on the same channel.
        """
        binding = self.inbox_routing.binding_for(
            self.inbox_routing.route_for(session_id, agent)
        )
        return [f"{binding.channel}:{binding.target}"] if binding.channel else []

    # -- connection hierarchy (UI-REFRESH §4) -----------------------------------
    def _persona_of(self, session_id: str, persona_id: Optional[str] = None) -> str:
        if persona_id:
            return persona_id
        record = self.session_store.load(session_id)
        return (record.agent if record else None) or self.personas.default_id()

    def effective_connectors(
        self, session_id: str, persona_id: Optional[str] = None
    ) -> set[str]:
        """The connectors effectively enabled for this session (§4.1): connected AND not muted by
        the session override / persona default. Drives the engine's connector-tool gating; seeds the
        persona defaults from the manifest on first read using the full connected set.
        """
        persona = self._persona_of(session_id, persona_id)
        connected = {c["name"] for c in connector_list(self.secrets) if c["connected"]}
        entry = self.personas.get(persona)
        manifest = entry.manifest if entry else None
        persona_defaults = self.persona_connections.defaults_for(
            persona, manifest, connected=connected
        )
        session_overrides = self.session_connections.get(session_id)
        return set(
            effective_connections(
                connected=connected,
                persona_defaults=persona_defaults,
                session_overrides=session_overrides,
            )
        )

    def _inbound_connector_allowed(self, session_id: str, connector: str) -> bool:
        """Whether an inbound message on `connector` should be DELIVERED to `session_id` (§4.3).

        Uses the SAME effective set as the engine's connector-tool gating so the inbound gate and the
        tool gate can never disagree (a muted connector is muted both ways, from the first message).
        """
        return connector in self.effective_connectors(session_id)

    # -- persona + session connection surfaces (UI-REFRESH §5/§6) ----------------
    @staticmethod
    def _workspace_kind(entry) -> str:
        """The persona's workspace requirement as a stable string for the GUI. Manifest-backed
        personas carry it verbatim (git|deliverable|none); builtins (which have no manifest) map
        family/needs_workspace into the SAME vocabulary so the frontend reads one enum:
        code-family → git, knowledge-family with a workspace → deliverable, none → none.
        """
        if entry.manifest is not None:
            return entry.manifest.workspace
        if not entry.needs_workspace:
            return "none"
        return "git" if entry.family == "code" else "deliverable"

    def _connected_connectors(self) -> set[str]:
        """The account-connected connector names (the first layer of the §4 hierarchy)."""
        return {c["name"] for c in connector_list(self.secrets) if c["connected"]}

    def _persona_default_connections(
        self, persona_id: str, manifest, connected: set[str]
    ) -> list[dict[str, Any]]:
        """The persona's default connector map (seeded from the manifest's connector recommends on
        first read, then user-editable) as a list, each annotated with account-connectedness.
        """
        defaults = self.persona_connections.defaults_for(
            persona_id, manifest, connected=connected
        )
        return [
            {"connector": c, "enabled": bool(enabled), "connected": c in connected}
            for c, enabled in defaults.items()
        ]

    def persona_detail(self, persona_id: str) -> Optional[dict[str, Any]]:
        """Identity + capabilities + recommends(+connected) + default connections for one persona
        (UI-REFRESH §5). Returns None for an unknown id (the route maps that to an error).
        """
        entry = self.personas.get(persona_id)
        if entry is None:
            return None
        manifest = entry.manifest
        connected = self._connected_connectors()
        recommends = [
            {
                "kind": rec.kind,
                "ref": rec.ref,
                "reason": rec.reason,
                "tier": rec.tier,
                "connected": rec.ref in connected,
            }
            for rec in (manifest.recommends if manifest else [])
        ]
        return {
            "id": entry.id,
            "name": entry.name,
            "icon": entry.icon,
            "tagline": entry.tagline,
            "description": manifest.description if manifest else "",
            "enabled": self.personas.is_enabled(entry.id),
            "tools": list(entry.tools),
            "recommended_models": list(manifest.recommended_models) if manifest else [],
            "default_permission_mode": (
                manifest.default_permission_mode if manifest else "interactive"
            ),
            "workspace": self._workspace_kind(entry),
            "recommends": recommends,
            "default_connections": self._persona_default_connections(
                persona_id, manifest, connected
            ),
        }

    def set_persona_connection(
        self, persona_id: str, connector: str, enabled: bool
    ) -> dict[str, Any]:
        """Set a persona-default connector on/off (UI-REFRESH §5). Seeds the manifest defaults
        first so the stored row stays complete (the edit overlays the full seed rather than
        collapsing the row to this one connector), then returns the refreshed default_connections
        so the client can re-render without a second GET."""
        entry = self.personas.get(persona_id)
        if entry is None:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        manifest = entry.manifest
        connected = self._connected_connectors()
        self.persona_connections.defaults_for(persona_id, manifest, connected=connected)
        self.persona_connections.set(persona_id, connector, bool(enabled))
        return {
            "ok": True,
            "default_connections": self._persona_default_connections(
                persona_id, manifest, connected
            ),
        }

    def set_persona_enabled(self, persona_id: str, enabled: bool) -> dict[str, Any]:
        """Flip a persona's enabled flag. Disabling also archives its real (unarchived,
        non-internal) sessions — disable means "put this coworker and its history away", so
        the persona's sidebar section disappears with it (owner call, 2026-07-04). Re-enabling
        never unarchives: that would overwrite the user's archive state; history returns one
        click at a time via the Show-archived disclosure. Raises KeyError for unknown ids.
        """
        self.personas.set_enabled(persona_id, enabled)
        archived = 0
        if not enabled:
            for r in self.session_store.list():
                if (
                    r.agent == persona_id
                    and not r.archived
                    and not r.session_id.startswith("__")
                ):
                    self.session_store.set_flags(r.session_id, archived=True)
                    archived += 1
        return {"ok": True, "archived_sessions": archived}

    def _connection_detail(
        self, session_id: str, connector: str, info: Optional[dict[str, Any]]
    ) -> str:
        """A short human description of WHY a connector is live for a session: the chat ids it's
        subscribed to on that platform, plus "DMs" if this is the designated DM session. Channel
        *names* would need the live adapter's resolve cache (not cheap here), so we show the chat
        ids; with no subscription/DM tie we fall back to the connector's title."""
        prefix = f"{connector}:"
        parts = [
            s.channel.split(":", 1)[1]
            for s in self.subscriptions.for_session(session_id)
            if s.channel.startswith(prefix)
        ]
        if self.dm_session() == session_id:
            parts.append("DMs")
        if parts:
            return " · ".join(parts)
        return (info or {}).get("title") or connector

    def session_connections_view(
        self, session_id: str, persona_id: Optional[str] = None
    ) -> dict[str, Any]:
        """The per-session connections drawer payload (UI-REFRESH §6): every account-connected
        connector with its effective on/off state (muted ones stay VISIBLE as off — a §4.2 toggle
        must never make a row vanish), the persona's connector recommends that aren't yet
        account-connected, and the attention count (= those unconnected recommends).

        ``persona_id`` is the caller's hint (the GUI knows the active persona). It matters for a
        brand-new session: no SessionRecord exists until the first turn persists, so without the
        hint the view would resolve to the DEFAULT persona and show its defaults/recommends —
        the owner's 2026-07-03 finding (a fresh Project Manager session rendered cowork's view).
        """
        persona = self._persona_of(session_id, persona_id)
        entry = self.personas.get(persona)
        manifest = entry.manifest if entry else None
        connectors = connector_list(self.secrets)
        by_name = {c["name"]: c for c in connectors}
        connected_names = {c["name"] for c in connectors if c["connected"]}
        effective = self.effective_connectors(session_id, persona)
        connected = [
            {
                "connector": name,
                "enabled": name in effective,
                "detail": self._connection_detail(session_id, name, by_name.get(name)),
            }
            for name in sorted(connected_names)
        ]
        recommended = [
            {
                "connector": rec.ref,
                "reason": rec.reason,
                "tier": rec.tier,
                "connected": False,
            }
            for rec in (manifest.recommends if manifest else [])
            if rec.kind == "connector" and rec.ref not in connected_names
        ]
        return {
            "connected": connected,
            "recommended": recommended,
            "attention": sum(1 for r in recommended if not r["connected"]),
        }

    def inbox_question_asker(self, session_id: str, agent: str):
        """The Unattended `ask_user` handler: turn the agent's question into an Inbox item and
        suspend until a human answers it (from the Inbox, or inline when they open the session).
        Also the default for background/self-wake runs (no live socket). Mirrors to a bound channel
        like the approver does."""

        async def ask(
            args: dict[str, Any], tool_call_id: Optional[str] = None
        ) -> dict[str, Any]:
            question = str(args.get("question", "")).strip()
            if not question:
                return {"answer": "", "error": "no question"}
            inbox_name = self.inbox_routing.route_for(session_id, agent)
            item = self.inbox.add_question(
                session_id,
                title=question,
                inbox=inbox_name,
                options=list(args.get("options") or []),
                allow_text=bool(args.get("allow_text", True)),
                multi=bool(args.get("multi", False)),
                tool_call_id=tool_call_id,
            )
            if (
                item.state != "pending"
            ):  # durable resume re-raised an already-answered prompt
                return {"answer": item.resolution or ""}
            self.persist_session(session_id)  # the pending tool call is now on disk
            await self.mirror_inbox_item(item)
            answer = await self.inbox.wait(item.id)
            return {"answer": answer}

        return ask

    def inbox_approver(self, session_id: str, agent: str):
        """Inbox-based approver — the default for no-socket runs (background, self-wake, durable
        resume). On resume the item already exists + is resolved, so wait returns at once.
        """

        async def approve(request):
            item = self.inbox.add_approval(
                session_id,
                f"运行 `{request.tool_name}`？",
                body=_approval_body(request),
                inbox=self.inbox_routing.route_for(session_id, agent),
                tool_call_id=getattr(request, "tool_call_id", None),
                data=self.approval_prompt_data(session_id, request),
            )
            if item.state == "pending":
                self.persist_session(session_id)
                await self.mirror_inbox_item(item)
            resolution = await self.inbox.wait(item.id)
            return self.approval_outcome(resolution, request, session_id)

        return approve

    def inbox_directory_requester(self, session_id: str, agent: str):
        async def request(args, tool_call_id=None):
            item = self.inbox.add_directory(
                session_id,
                "授权访问某个文件夹？",
                body=str(args.get("reason", "")),
                inbox=self.inbox_routing.route_for(session_id, agent),
                data={
                    "path": str(args.get("path", "")),
                    "writable": bool(args.get("writable", False)),
                },
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                self.persist_session(session_id)
                await self.mirror_inbox_item(item)
            resp = _parse_inbox_json(await self.inbox.wait(item.id))
            if not resp.get("granted"):
                return {"granted": False, "reason": "用户拒绝了该请求"}
            path = (resp.get("path") or args.get("path") or "").strip()
            if not path:
                return {"granted": False, "error": "未提供任何目录"}
            writable = bool(resp.get("writable", args.get("writable", False)))
            res = self.add_root(session_id, path, writable)
            if not res.get("ok"):
                return {
                    "granted": False,
                    "error": res.get("error", "无法授权访问"),
                }
            return {"granted": True, "path": path, "writable": writable}

        return request

    def inbox_plan_approver(self, session_id: str, agent: str):
        async def approve(args, tool_call_id=None):
            item = self.inbox.add_plan(
                session_id,
                "批准该计划？",
                body=str(args.get("plan", "")),
                inbox=self.inbox_routing.route_for(session_id, agent),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                self.persist_session(session_id)
                await self.mirror_inbox_item(item)
            resp = _parse_inbox_json(await self.inbox.wait(item.id))
            if not resp.get("approved"):
                return {
                    "approved": False,
                    "feedback": resp.get("feedback") or "the user rejected the plan",
                }
            return {"approved": True, "mode": resp.get("mode") or "interactive"}

        return approve

    def persist_session(self, session_id: str) -> None:
        """Save the cached engine's thread (so a prompt's pending tool call survives a crash)."""
        engine = self._engines.get(session_id)
        if engine is not None:
            self.save(session_id, engine)

    async def resolve_inbox(self, item_id: str, resolution: str) -> bool:
        """Resolve an Inbox item from any surface (REST / Slack button / channel reply). If the
        asking agent is still suspended live, that await handles it. Otherwise the process restarted
        (or the engine was evicted) while blocked → durably resume: rebuild the engine from the
        saved thread and continue the turn."""
        item = self.inbox.get(item_id)
        ok = self.inbox.resolve(item_id, resolution)
        if not ok or item is None:
            return ok
        if not self.is_running(item.session_id):
            await self._durable_resume(item)
        return ok

    async def _durable_resume(self, item) -> None:
        if not getattr(item, "tool_call_id", None):
            return  # nothing to reconstruct (legacy item) — best-effort: leave it
        engine = self.get_engine(item.session_id)
        if engine is None or not hasattr(engine, "resume"):
            return
        self.mark_running(item.session_id)
        try:
            async for _event in engine.resume():
                pass
            self.save(item.session_id, engine)
        finally:
            self.mark_idle(item.session_id)

    # -- MCP --------------------------------------------------------------------
    async def prepare_mcp_tools(
        self, session_id: str, *, workspace: Optional[str] = None, agent: str = "code"
    ) -> list[Any]:
        """Connect enabled MCP servers (global + workspace) and return their tool callables.

        Called from the async WS handler before `get_engine`; no-op if the engine is already
        built (its MCP tools are attached). Servers that fail to connect are skipped.
        """
        if session_id in self._engines:
            return []
        from ..connectors.descriptors import get_descriptor
        from ..connectors.tool_defs import (
            approval_for_tool,
            mcp_tool_defs,
            tool_enabled,
        )

        from ..mcp import oauth as mcp_oauth

        ws = self.engine_workspace(session_id, workspace=workspace, agent=agent)
        loop = asyncio.get_running_loop()
        effective: Optional[set[str]] = None  # computed lazily, once
        out: list[Any] = []
        for server in load_mcp_servers(
            ws,
            secrets=self.secrets,
            workspace_trusted=self._mcp_workspace_trusted(ws),
        ):
            if not server.enabled:
                continue
            if server.auth == "oauth" and not mcp_oauth.has_tokens(
                server.name, self.secrets
            ):
                # NEVER start an interactive OAuth flow from a turn: a token-less
                # server here would open a browser and block every session for the
                # full flow timeout (owner-hit 2026-07-20 — a failed one-click's
                # leftover config froze all new sessions). Flows start only from an
                # explicit connect in Settings/Connectors.
                continue
            descriptor = get_descriptor(server.name)
            backed = descriptor is not None and bool(descriptor.mcp_url)
            if backed:
                # Connector-backed server: obey the same gates as connector tools —
                # the session's effective connector set and the per-tool toggles.
                # The descriptor's PIN is authoritative over whatever the config
                # file says (drift can only ever shrink the surface).
                if effective is None:
                    effective = self.effective_connectors(session_id, agent)
                if server.name not in effective:
                    continue
                prefix = f"mcp__{server.name}__"
                server.include_tools = [
                    t.name.removeprefix(prefix)
                    for t in mcp_tool_defs(server.name)
                    if tool_enabled(self.secrets, server.name, t.name)
                ]
            try:
                conn = await self.mcp.ensure(server)
            except Exception as exc:
                if mcp_oauth.is_auth_required(exc):
                    # Stored tokens no longer refresh (vendor rotated/expired
                    # them) — the non-interactive connect refused to open a
                    # browser. Record it so the MCP page shows WHY the server is
                    # dark; the session just runs without its tools.
                    self._mcp_errors[server.name] = (
                        "sign-in required — reconnect this server from its page"
                    )
                    logger.info(
                        "mcp %s needs re-auth; skipped for this session", server.name
                    )
                # else: bad command / unreachable url — skip, don't break the session
                continue
            callables = build_callables(
                server,
                conn.tools,
                lambda tool, args, name=server.name: self.mcp.call(name, tool, args),
                loop,
            )
            if backed:
                # Per-tool approval from the pinned read/write classification
                # (server-level requires_approval is off for backed servers);
                # anything unclassified stays approval-gated — fail closed.
                for fn in callables:
                    fn.__aisuite_tool_metadata__.requires_approval = approval_for_tool(
                        fn.__aisuite_tool_metadata__.name, default=True
                    )
            out.extend(callables)
        return out

    def list_mcp(self) -> list[dict[str, Any]]:
        """Servers from the global config + connection status (does not connect)."""
        from ..mcp import oauth as mcp_oauth

        from ..connectors.descriptors import get_descriptor

        out = []
        for name, raw in read_global().items():
            d = get_descriptor(name)
            if d is not None and d.mcp_url:
                # Connector-backed server: surfaced on the Connectors page (its
                # connect/disconnect lifecycle lives there), not in the MCP tab.
                continue
            connected = name in self.mcp._conns
            is_oauth = str(raw.get("auth", "")).lower() == "oauth"
            if connected:
                status = "connected"
            elif not raw.get("enabled", True):
                status = "disabled"
            elif name in self._mcp_authorizing:
                status = "authorizing"
            elif is_oauth and not mcp_oauth.has_tokens(name, self.secrets):
                status = "needs_auth"
            else:
                status = "configured"
            out.append(
                {
                    "name": name,
                    "enabled": bool(raw.get("enabled", True)),
                    "transport": (
                        "http"
                        if (
                            raw.get("url")
                            or str(raw.get("type", "")).lower()
                            in {"http", "sse", "streamable-http"}
                        )
                        else "stdio"
                    ),
                    "requires_approval": bool(raw.get("requires_approval", True)),
                    "auth": "oauth" if is_oauth else None,
                    "status": status,
                    "last_error": self._mcp_errors.get(name),
                    "tool_count": (
                        len(self.mcp._conns[name].tools) if connected else None
                    ),
                    "config": _redact(raw),
                }
            )
        return out

    async def connect_mcp(self, name: str) -> dict[str, Any]:
        """Connect one server NOW — for OAuth servers this may open the browser and wait
        for the loopback callback, so callers run it as a background task and watch
        list_mcp for the status flip."""
        for server in load_mcp_servers(
            self.default_workspace,
            secrets=self.secrets,
            workspace_trusted=self._mcp_workspace_trusted(self.default_workspace),
        ):
            if server.name != name:
                continue
            self._mcp_authorizing.add(name)
            self._mcp_errors.pop(name, None)
            try:
                # The ONE place a browser sign-in may start: an explicit connect.
                conn = await self.mcp.ensure(server, interactive=True)
                return {"ok": True, "tools": len(conn.tools)}
            except Exception as exc:
                self._mcp_errors[name] = str(exc) or exc.__class__.__name__
                return {"ok": False, "error": self._mcp_errors[name]}
            finally:
                self._mcp_authorizing.discard(name)
        return {"ok": False, "error": f"unknown MCP server: {name}"}

    async def mcp_connect_connector(self, name: str) -> dict[str, Any]:
        """One-click connect for an MCP-BACKED connector (descriptor.mcp_url): seed
        the global server entry pinned to the curated allowlist, run the browser
        OAuth flow, and mark the connector profile `mode: "mcp"` on success."""
        from ..connectors.descriptors import get_descriptor
        from ..connectors.tool_defs import mcp_pinned_tools

        d = get_descriptor(name)
        if d is None or not d.mcp_url:
            return {"ok": False, "error": f"{name} has no MCP connect path"}
        put_global_server(
            name,
            {
                "url": d.mcp_url,
                "auth": "oauth",
                # Server-level approval off: writes gate per-tool via the pinned
                # read/write classification (prepare_mcp_tools); unknown vendor
                # tools never load at all (include_tools).
                "requires_approval": False,
                "include_tools": mcp_pinned_tools(name),
                "enabled": True,
            },
        )
        result = await self.connect_mcp(name)
        if result.get("ok"):
            profile = self.secrets.get(f"{name}:default") or {}
            self.secrets.put(
                f"{name}:default", {**profile, "mode": "mcp", "enabled": True}
            )
        else:
            # A failed connect must take its seeded config with it: an enabled
            # oauth entry with no tokens lingers forever (nothing owns it once
            # the descriptor's mcp_url is gone) and re-arms at every session
            # start — the owner-hit asana leftover, 2026-07-20.
            delete_global_server(name)
        return result

    async def signout_mcp(self, name: str) -> dict[str, Any]:
        """Drop the live connection (if any) and forget the stored OAuth tokens."""
        from ..mcp import oauth as mcp_oauth

        conn = self.mcp._conns.get(name)
        if conn is not None:
            conn.shutdown.set()
        self._mcp_errors.pop(name, None)
        removed = mcp_oauth.sign_out(name, self.secrets)
        return {"ok": True, "had_tokens": removed}

    def add_mcp(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        put_global_server(name, config)
        return {"ok": True, "name": name}

    def patch_mcp(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        ok = patch_global_server(name, changes)
        return {"ok": ok, "name": name}

    def delete_mcp(self, name: str) -> dict[str, Any]:
        ok = delete_global_server(name)
        return {"ok": ok, "name": name}

    async def mcp_tools(self, name: str) -> dict[str, Any]:
        """Connect one server and list its tools (name + description)."""
        for server in load_mcp_servers(
            self.default_workspace,
            secrets=self.secrets,
            workspace_trusted=self._mcp_workspace_trusted(self.default_workspace),
        ):
            if server.name == name:
                try:
                    conn = await self.mcp.ensure(server)
                except Exception as exc:
                    return {"name": name, "ok": False, "error": str(exc), "tools": []}
                return {
                    "name": name,
                    "ok": True,
                    "tools": [
                        {"name": t.name, "description": getattr(t, "description", "")}
                        for t in conn.tools
                    ],
                }
        return {"name": name, "ok": False, "error": "unknown server", "tools": []}

    async def reload_mcp(self) -> dict[str, Any]:
        """Drop live MCP connections so new sessions reconnect with fresh config."""
        await self.mcp.aclose()
        return {"ok": True}

    # -- connectors -------------------------------------------------------------
    async def _wechat_ilink_confirmed(self, _account_id: str) -> None:
        """Best-effort hot refresh after QR confirmation saves an account."""
        await self.refresh_gateway()

    def _wechat_ilink_runtime_status(self) -> dict[str, dict[str, Any]]:
        adapter = (
            self.gateway._adapters.get("wechat_ilink")
            if self.gateway is not None
            else None
        )
        snapshot = getattr(adapter, "status", None)
        if not callable(snapshot):
            return {}
        status = snapshot()
        accounts = status.get("accounts") if isinstance(status, dict) else None
        return accounts if isinstance(accounts, dict) else {}

    async def create_wechat_ilink_qr(
        self, *, reauth_account_id: str = ""
    ) -> dict[str, Any]:
        if reauth_account_id:
            from ..connectors.wechat_ilink.profiles import (
                ProfileError,
                get_account,
            )

            try:
                if get_account(self.secrets, reauth_account_id) is None:
                    return {"ok": False, "error": "account not connected"}
            except ProfileError:
                return {"ok": False, "error": "invalid account id"}
        try:
            return {
                "ok": True,
                **await self.wechat_ilink_qr.create(
                    reauth_account_id=reauth_account_id
                ),
            }
        except Exception:
            return {"ok": False, "error": "wechat_ilink QR code unavailable"}

    async def get_wechat_ilink_qr(self, attempt_id: str) -> dict[str, Any]:
        attempt = await self.wechat_ilink_qr.get(str(attempt_id).strip())
        if attempt is None:
            return {"ok": False, "error": "unknown QR attempt"}
        return {"ok": True, **attempt}

    async def cancel_wechat_ilink_qr(self, attempt_id: str) -> dict[str, Any]:
        if not await self.wechat_ilink_qr.cancel(str(attempt_id).strip()):
            return {"ok": False, "error": "unknown QR attempt"}
        return {"ok": True}

    def wechat_ilink_accounts(self) -> dict[str, Any]:
        from ..connectors.wechat_ilink.profiles import account_rows

        runtime = self._wechat_ilink_runtime_status()
        accounts: list[dict[str, Any]] = []
        for row in account_rows(self.secrets):
            live = runtime.get(row["account_id"], {})
            state = str(live.get("state") or "")
            if not state:
                state = "auth_required" if row.get("needs_reauth") else "offline"
            accounts.append(
                {
                    **row,
                    "state": state,
                    "retry_count": int(live.get("retry_count") or 0),
                    "last_event_at": live.get("last_event_at"),
                    "last_error": str(live.get("last_error") or ""),
                    "needs_reauth": bool(
                        row.get("needs_reauth")
                        or live.get("needs_reauth")
                        or state == "auth_required"
                    ),
                }
            )
        return {"ok": True, "accounts": accounts}

    def wechat_ilink_status(self) -> dict[str, Any]:
        accounts = self.wechat_ilink_accounts()["accounts"]
        states = {str(a.get("state") or "offline") for a in accounts}
        if "live" in states:
            state = "live"
        elif "reconnecting" in states or "connecting" in states:
            state = "reconnecting"
        elif "auth_required" in states:
            state = "auth_required"
        else:
            state = "offline"
        return {"ok": True, "state": state, "accounts": accounts}

    async def disconnect_wechat_ilink_all(self) -> dict[str, Any]:
        """Cancel QR workers before deleting every local iLink credential."""
        await self.wechat_ilink_qr.aclose()
        result = disconnect_connector(self.secrets, "wechat_ilink")
        await self.refresh_gateway()
        return result

    async def disconnect_wechat_ilink_account(
        self, account_id: str
    ) -> dict[str, Any]:
        from ..connectors.wechat_ilink.profiles import ProfileError, delete_account

        account_id = str(account_id).strip()
        try:
            await self.wechat_ilink_qr.cancel_for_account(account_id)
            deleted = delete_account(self.secrets, account_id)
        except ProfileError:
            return {"ok": False, "error": "invalid account id"}
        if not deleted:
            return {"ok": False, "error": "account not connected"}
        await self.refresh_gateway()
        return {
            "ok": True,
            "remaining_accounts": len(self.wechat_ilink_accounts()["accounts"]),
        }

    async def set_wechat_ilink_default(self, account_id: str) -> dict[str, Any]:
        from ..connectors.wechat_ilink.profiles import ProfileError, set_default

        try:
            changed = set_default(self.secrets, str(account_id).strip())
        except ProfileError:
            return {"ok": False, "error": "invalid account id"}
        return {"ok": changed, **({} if changed else {"error": "account not connected"})}

    def list_connectors(self) -> list[dict[str, Any]]:
        # Enrich two-way connectors with the live gateway's recently-seen senders, so the Connectors
        # tab can manage the allow-list inline (each recent sender flagged authorized or not).
        connectors = connector_list(self.secrets)
        for c in connectors:
            if not (c.get("two_way") and c.get("connected")):
                continue
            allowed = set(c.get("allowed_users") or [])
            # Per-workspace/account allow-lists — a sender is judged against ITS
            # tenant's list; the flat list only governs team-less events.
            team_rows = list(c.get("workspaces") or [])
            if c.get("name") == "wechat_ilink":
                runtime = {
                    a["account_id"]: a
                    for a in self.wechat_ilink_accounts()["accounts"]
                }
                for account in c.get("accounts") or []:
                    account.update(runtime.get(account.get("account_id"), {}))
                    account["allowed_user_names"] = {
                        u: self._people.get(f"{c['name']}:{u}")
                        for u in (account.get("allowed_users") or [])
                    }
                    team_rows.append(
                        {
                            **account,
                            "team_id": account.get("account_id"),
                        }
                    )
                c["status"] = self.wechat_ilink_status()
            team_allowed = {
                row["team_id"]: set(row.get("allowed_users") or [])
                for row in team_rows
                if row.get("team_id")
            }
            recent = self.gateway.recent_senders(c["name"]) if self.gateway else []
            for r in recent:
                team = r.get("team_id")
                pool = team_allowed.get(team, set()) if team else allowed
                r["authorized"] = r.get("user_id") in pool
                # Backfill from the people directory (an event may predate name scopes).
                r["user_name"] = r.get("user_name") or self._people.get(
                    f"{c['name']}:{r.get('user_id')}"
                )
            c["recent"] = recent
            # Parked unauthorized messages (§19) — the connector page resolves them inline.
            c["unauthorized"] = self.parked.list(c["name"])
            # Allow-list display names from the people directory (ids stay the source of truth).
            c["allowed_user_names"] = {
                u: self._people.get(f"{c['name']}:{u}")
                for u in (c.get("allowed_users") or [])
            }
            c["approval_owner_names"] = {
                u: self._people.get(f"{c['name']}:{u}")
                for u in (c.get("approval_owner_ids") or [])
            }
            for row in team_rows:
                row["allowed_user_names"] = {
                    u: self._people.get(f"{c['name']}:{u}")
                    for u in (row.get("allowed_users") or [])
                }
                if row in (c.get("workspaces") or []):
                    row["approval_owner_names"] = {
                        u: self._people.get(f"{c['name']}:{u}")
                        for u in (row.get("approval_owner_ids") or [])
                    }
        return connectors

    def connect_connector(
        self, name: str, fields: dict[str, Any], *, acknowledged: bool = False
    ) -> dict[str, Any]:
        # validates the token by a live API call (sync httpx) — run off the event loop
        return connect_connector(self.secrets, name, fields, acknowledged=acknowledged)

    def set_experimental_connectors(self, value: bool) -> dict[str, Any]:
        return set_experimental_enabled(self.secrets, value)

    def disconnect_connector(self, name: str) -> dict[str, Any]:
        # MCP-backed profile: drop the live server connection before the tokens go.
        conn = self.mcp._conns.get(name)
        if conn is not None:
            conn.shutdown.set()
        return disconnect_connector(self.secrets, name)

    def update_connector_tools(
        self, name: str, enabled: dict[str, Any]
    ) -> dict[str, Any]:
        return update_connector_tools(self.secrets, name, enabled)

    def list_audit(
        self,
        *,
        limit: int = 100,
        session_id: Optional[str] = None,
        connector: Optional[str] = None,
        tool: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self.audit_store.list(
            limit=limit, session_id=session_id, connector=connector, tool=tool
        )

    def browser_state(self) -> dict[str, Any]:
        return browser_state()

    def browser_screenshot(self) -> dict[str, Any]:
        return browser_take_screenshot()

    def browser_close(self) -> dict[str, Any]:
        return browser_close_session()

    # -- Browser login state (E5) ----------------------------------------------

    def _resolve_login_state_for_url(self, url: str) -> Optional[str]:
        """Resolver wired into browser_automation: URL host → abs storageState path or None.

        Also normalizes pasted-cookie entries: if a login was captured via cookie paste
        (no storageState.json yet), lazily convert cookies.json → storageState.json on
        first use so browser_open_url has a single code path.
        """
        from ..browser_logins import match_login_for_url

        login = match_login_for_url(url, self.browser_logins.list())
        if login is None or not login.has_state:
            return None
        return self._login_state_abs_path(login)

    def _login_state_abs_path(self, login) -> Optional[str]:
        """Absolute path to a usable storageState.json for ``login``, converting cookies if needed."""
        from ..browser_login_capture import cookies_to_storage_state

        state = state_dir()
        # Preferred: a Playwright-captured storageState.json already on disk.
        if login.storage_state_path:
            p = state / login.storage_state_path
            if p.is_file():
                return str(p)
        # Fallback: convert pasted cookies.json into a storageState.json (lazy, once).
        if login.cookie_path:
            cookies_p = state / login.cookie_path
            if cookies_p.is_file():
                state_p = cookies_p.parent / "storageState.json"
                if cookies_to_storage_state(cookies_p, state_p):
                    return str(state_p)
        return None

    def list_browser_logins(self) -> list[dict[str, Any]]:
        from ..browser_login_capture import inspect_login_expiry

        base = state_dir()
        out = []
        for l in self.browser_logins.list():
            d = l.to_dict()
            d["expiry"] = inspect_login_expiry(l, base)
            out.append(d)
        return out

    def add_browser_login(self, url: str, label: str, *, mode: str = "playwright") -> dict[str, Any]:
        from ..browser_logins import BrowserLoginEntry, make_id, safe_id

        if not url:
            return {"ok": False, "error": "url is required"}
        login_id = safe_id(make_id(url))
        entry = BrowserLoginEntry(id=login_id, url=url, label=label or url, mode=mode)
        self.browser_logins.add(entry)
        # Tell the caller whether Playwright is available so the frontend can pick the
        # right capture flow (headed window vs cookie paste) without a separate probe.
        # Surface the probe error too: when Playwright is *installed but broken* the
        # boolean alone reads as "not installed" and the "install playwright" hint is
        # misleading — the error string tells the user what's actually wrong.
        from ..browser_login_capture import (
            playwright_probe_error,
            try_playwright_available,
        )

        result: dict[str, Any] = {
            "ok": True,
            "id": login_id,
            "entry": entry.to_dict(),
            "playwright_available": try_playwright_available(),
        }
        probe_err = playwright_probe_error()
        if probe_err:
            result["playwright_error"] = probe_err
        return result

    def begin_browser_login_capture(self, login_id: str) -> dict[str, Any]:
        from ..browser_logins import safe_id
        from ..browser_login_capture import begin_playwright_capture, try_playwright_available

        login = self.browser_logins.get(safe_id(login_id))
        if login is None:
            return {"ok": False, "error": f"login {login_id!r} not found"}
        if not try_playwright_available():
            return {"ok": False, "fallback": "cookies", "id": login.id}
        return begin_playwright_capture(login.url, login.id)

    def confirm_browser_login_capture(self, login_id: str) -> dict[str, Any]:
        from ..browser_logins import safe_id
        from ..browser_login_capture import confirm_playwright_capture

        login = self.browser_logins.get(safe_id(login_id))
        if login is None:
            return {"ok": False, "error": f"login {login_id!r} not found"}
        result = confirm_playwright_capture(self._browser_profiles_dir, login.id)
        if result.get("ok"):
            self.browser_logins.update(login.id, {
                "mode": "playwright",
                "storage_state_path": result.get("storage_state_path", ""),
                "has_state": True,
                "captured_at": result.get("captured_at", ""),
            })
        return result

    def cancel_browser_login_capture(self) -> dict[str, Any]:
        from ..browser_login_capture import cancel_playwright_capture

        return cancel_playwright_capture()

    def browser_login_capture_state(self) -> dict[str, Any]:
        from ..browser_login_capture import capture_session_state

        return capture_session_state()

    def save_browser_login_cookies(self, login_id: str, cookie_json: str) -> dict[str, Any]:
        from ..browser_logins import safe_id
        from ..browser_login_capture import capture_via_cookies

        login = self.browser_logins.get(safe_id(login_id))
        if login is None:
            return {"ok": False, "error": f"login {login_id!r} not found"}
        result = capture_via_cookies(login.id, cookie_json, self._browser_profiles_dir)
        if result.get("ok"):
            self.browser_logins.update(login.id, {
                "mode": "cookies",
                "cookie_path": result.get("cookie_path", ""),
                "has_state": True,
                "captured_at": result.get("captured_at", ""),
            })
        return result

    def remove_browser_login(self, login_id: str) -> dict[str, Any]:
        from ..browser_logins import safe_id
        import shutil

        login = self.browser_logins.get(safe_id(login_id))
        if login is None:
            return {"ok": False, "error": f"login {login_id!r} not found"}
        # Delete the profile dir (storageState.json + cookies.json).
        profile = self._browser_profiles_dir / login.id
        if profile.is_dir():
            shutil.rmtree(profile, ignore_errors=True)
        self.browser_logins.remove(login.id)
        return {"ok": True}

    def export_browser_logins(self) -> dict[str, Any]:
        """Export all login entries + their persisted state files as a single JSON blob.

        The result is a portable backup: registry entries (id/url/label/mode/...) plus the
        inlined contents of each entry's storageState.json / cookies.json. The frontend
        triggers a file download of this. ``version`` tags the format so future importers
        can migrate.

        Security: only reads the canonical ``storageState.json`` / ``cookies.json`` files
        derived from the registry's ``safe_id``. Containment uses ``is_relative_to`` on the
        resolved path (not string ``startswith``) and rejects symlinks, so a tampered entry
        whose stored path points outside the state dir can't exfiltrate arbitrary files.
        """
        import json

        base = Path(state_dir()).resolve()
        entries = []
        for l in self.browser_logins.list():
            entry = l.to_dict()
            files: dict[str, str] = {}
            for rel in (l.storage_state_path, l.cookie_path):
                if not rel:
                    continue
                # Resolve and verify containment: the file must live under the state dir.
                try:
                    p = (base / rel).resolve()
                    p.relative_to(base)  # reject path escape
                except (ValueError, OSError):
                    continue  # entry points outside state dir — skip, don't read it
                if p.is_symlink():
                    continue  # reparse-point escape — don't follow
                if p.is_file():
                    try:
                        files[rel] = p.read_text(encoding="utf-8")
                    except Exception:
                        pass
            entry["_files"] = files
            entries.append(entry)
        return {"version": 1, "logins": entries}

    def import_browser_logins(self, payload: str) -> dict[str, Any]:
        """Import a previously-exported login backup. Restores registry entries + state files.

        Existing entries with the same id are overwritten (same overwrite-on-add semantics as
        the registry). State files are written under ``browser_profiles/<id>/`` before the
        registry entry is added, so has_state is truthful from the moment of import.

        Security: imported paths are NOT trusted. The canonical filenames
        (``storageState.json`` / ``cookies.json``) under ``browser_profiles/<safe_id>/`` are
        the only files written, derived from the validated ``safe_id`` — not from the
        backup's ``_files`` keys. Containment uses ``Path.is_relative_to()`` (not string
        ``startswith``, which a same-prefix sibling can bypass), symlinks/reparse points are
        rejected, and writes are atomic + user-private via ``write_private_text``.
        """
        import json

        from ..browser_logins import BrowserLoginEntry, safe_id
        from ..secrets import write_private_text

        # Bounds: a backup with thousands of files or a single oversized state is either
        # corrupt or an attempt to exhaust disk / memory during parse.
        _MAX_FILES_PER_LOGIN = 2  # storageState.json + cookies.json — nothing else is canonical
        _MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB per state file (cookies/state are small JSON)

        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
        except Exception as exc:
            return {"ok": False, "error": f"invalid JSON: {exc}"}
        if not isinstance(data, dict) or not isinstance(data.get("logins"), list):
            return {"ok": False, "error": "expected {version, logins: [...]}"}

        base = Path(state_dir()).resolve()
        imported = 0
        for raw in data["logins"]:
            if not isinstance(raw, dict):
                continue
            try:
                login_id = safe_id(str(raw.get("id") or ""))
            except ValueError:
                continue
            files = raw.get("_files") or {}
            if not isinstance(files, dict) or len(files) > _MAX_FILES_PER_LOGIN:
                continue  # too many files — refuse rather than write the overflow
            # Write state files first. The ONLY canonical destinations are under
            # browser_profiles/<safe_id>/, derived from the validated id — never from the
            # backup's rel-path keys (those are display-only metadata from the export).
            profile_root = (base / "browser_profiles" / login_id).resolve()
            try:
                profile_root.relative_to(base)  # containment: profile dir must stay under state
            except ValueError:
                continue
            if profile_root.is_symlink():  # reject reparse-point escape
                continue
            # Accept only the two canonical filenames regardless of what _files claims.
            canonical = {"storageState.json", "cookies.json"}
            for rel, content in files.items():
                if not isinstance(rel, str) or not isinstance(content, str):
                    continue
                # The basename must be one of the canonical files; ignore any other path.
                name = Path(rel).name
                if name not in canonical:
                    continue
                if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
                    continue  # oversized — skip, don't write a partial/malicious blob
                dest = (profile_root / name).resolve()
                # Final containment + symlink check on the resolved destination.
                try:
                    dest.relative_to(base)
                except ValueError:
                    continue
                if dest.is_symlink():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                write_private_text(dest, content)  # atomic + user-private (0600/ACL)
            # Rebuild the registry entry (drop the _files key).
            entry_raw = {k: v for k, v in raw.items() if k != "_files"}
            entry = BrowserLoginEntry.from_dict(entry_raw)
            self.browser_logins.add(entry)
            imported += 1
        return {"ok": True, "imported": imported}

    def dhp_logins_health(self, slug: str) -> dict[str, Any]:
        """For each site-login the spec requires, report whether a captured session exists."""
        try:
            spec = self.dhp_registry.get_spec(slug)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        logins = self.browser_logins.list()
        # Match by host: a spec browser_login entry {url,label} is satisfied if any
        # captured login's host matches.
        from ..browser_logins import match_login_for_url
        from ..browser_login_capture import inspect_login_expiry

        base = state_dir()
        out = []
        for entry in spec.browser_login:
            url = str((entry or {}).get("url") or "")
            label = str((entry or {}).get("label") or url)
            match = match_login_for_url(url, logins) if url else None
            out.append({
                "url": url,
                "label": label,
                "logged_in": bool(match and match.has_state),
                "expiry": inspect_login_expiry(match, base) if (match and match.has_state) else {"status": "no_state"},
            })
        return {"ok": True, "items": out}

    def list_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        record = self.session_store.load(session_id)
        workspace = record.workspace if record else self.default_workspace
        if not workspace:
            return []
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        suffixes = {
            ".md",
            ".markdown",
            ".html",
            ".htm",
            ".txt",
            ".json",
            ".csv",
            ".tsv",
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".css",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
            ".pdf",
            ".xlsx",
            ".xls",
            ".pptx",
            ".ppt",
            ".pptm",
            ".docx",
            ".doc",
            ".docm",
        }
        # os.walk with in-place pruning, NOT rglob: rglob descends first and filters after,
        # so a home-directory workspace walked into ~/Library and tripped the macOS App Data
        # TCC prompt ("OpenWorker would like to access data from other apps") on every turn.
        # Pruning here means those directories are never entered at all.
        from ..tools.search import OS_DATA_DIRS

        skip = {"node_modules", "target", "dist", "__pycache__"} | OS_DATA_DIRS
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
            for name in files:
                if name.startswith("."):
                    continue
                path = Path(dirpath) / name
                if path.suffix.lower() not in suffixes:
                    continue
                try:
                    st = path.stat()
                    if not path.is_file():
                        continue
                    out.append(
                        {
                            "path": str(path.relative_to(root)),
                            # Absolute path for "Copy path" — the relative one is useless
                            # outside the app (tester catch 2026-07-12: it copied just the
                            # filename).
                            "abs_path": str(path),
                            "name": path.name,
                            "kind": _artifact_kind(path),
                            "size": st.st_size,
                            "modified_at": st.st_mtime,
                        }
                    )
                except OSError:
                    continue
        out.sort(key=lambda a: a["modified_at"], reverse=True)
        return out[:80]

    MAX_BINARY_PREVIEW = 25 * 1024 * 1024  # base64-over-JSON gets heavy past this

    def _artifact_target(
        self, session_id: str, path: str, *, allow_dir: bool = False
    ) -> tuple[Optional[Path], Optional[str]]:
        """Resolve an artifact path under the session's workspace, or (None, error)."""
        record = self.session_store.load(session_id)
        workspace = record.workspace if record else self.default_workspace
        if not workspace:
            return None, "no workspace"
        root = Path(workspace).expanduser().resolve()
        target = (root / path).expanduser().resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None, "path escapes workspace"
        if allow_dir and target.is_dir():
            return target, None
        if not target.is_file():
            return None, (
                "该文件已不在会话文件夹中——可能已被移动或删除。"
            )
        return target, None

    def read_artifact(self, session_id: str, path: str) -> dict[str, Any]:
        # Folders are readable too (a model sometimes links a whole package, e.g. a skill
        # build dir): return a listing the viewer can render instead of a dead end.
        target, err = self._artifact_target(session_id, path, allow_dir=True)
        if target is None:
            return {"ok": False, "error": err}
        if target.is_dir():
            entries: list[dict[str, Any]] = []
            try:
                children = sorted(
                    target.iterdir(), key=lambda c: (c.is_file(), c.name.lower())
                )
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            for child in children[:500]:
                try:
                    size = 0 if child.is_dir() else child.stat().st_size
                except OSError:
                    continue
                entries.append({"name": child.name, "dir": child.is_dir(), "size": size})
            return {"ok": True, "path": path, "kind": "folder", "entries": entries}
        kind = _artifact_kind(target)
        if kind == "office":
            # PowerPoint/Word binaries can't be previewed inline; the UI offers
            # "Open in default app" instead of trying to render them.
            return {"ok": True, "path": path, "kind": "office"}
        if kind in ("image", "pdf", "sheet"):
            import base64

            if target.stat().st_size > self.MAX_BINARY_PREVIEW:
                return {
                    "ok": False,
                    "error": "file too large to preview — use Reveal to open it",
                }
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
                ".pdf": "application/pdf",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".xls": "application/vnd.ms-excel",
            }.get(target.suffix.lower(), "application/octet-stream")
            data = base64.b64encode(target.read_bytes()).decode("ascii")
            return {
                "ok": True,
                "path": path,
                "kind": kind,
                "data_url": f"data:{mime};base64,{data}",
            }
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "binary file cannot be previewed"}
        return {
            "ok": True,
            "path": path,
            "kind": kind,
            "content": text[:500000],
            "truncated": len(text) > 500000,
        }

    def reveal_artifact(
        self, session_id: str, path: str, mode: str = "reveal"
    ) -> dict[str, Any]:
        """Show the file in the OS file manager (`reveal`) or open it with its default app
        (`open`). The server runs on the user's machine in both desktop and browser builds, so
        this is local. Cross-platform: macOS `open`, Windows Explorer/ShellExecute, Linux
        `xdg-open`."""
        import os
        import subprocess
        import sys

        target, err = self._artifact_target(session_id, path, allow_dir=True)
        if target is None:
            return {"ok": False, "error": err}
        # A folder "opens" as itself in the file manager, whatever the mode.
        is_dir = target.is_dir()
        try:
            if sys.platform == "darwin":
                args = (
                    ["open", "-R", str(target)]
                    if mode == "reveal" and not is_dir
                    else ["open", str(target)]
                )
                subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            elif sys.platform == "win32":
                if mode == "reveal" and not is_dir:
                    # Explorer wants the path glued to the switch: /select,<path>
                    subprocess.Popen(["explorer", f"/select,{target}"])
                else:
                    os.startfile(str(target))  # type: ignore[attr-defined]  # open in default app
            else:  # Linux/BSD
                tgt = str(target.parent) if mode == "reveal" and not is_dir else str(target)
                subprocess.Popen(
                    ["xdg-open", tgt],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    # -- web search -------------------------------------------------------------
    def get_web_search(self) -> dict[str, Any]:
        from ..config import load_config
        from ..web import provider_names

        profile = self.secrets.get("web_search:default") or {}
        provider = (
            profile.get("provider") or load_config().web_search_provider or "duckduckgo"
        )
        return {
            "provider": provider,
            "has_key": bool(profile.get("api_key")),
            "providers": provider_names(),
        }

    def set_web_search(
        self, provider: str, api_key: Optional[str] = None
    ) -> dict[str, Any]:
        from ..web import provider_names

        if provider not in provider_names():
            return {"ok": False, "error": f"unknown provider: {provider}"}
        profile: dict[str, Any] = {"provider": provider}
        if api_key:
            profile["api_key"] = api_key
        self.secrets.put("web_search:default", profile)
        return {"ok": True, "provider": provider}

    # -- model providers (OpenAI, Ollama, …) ------------------------------------
    def get_providers(self) -> list[dict[str, Any]]:
        """Descriptor + per-provider status for the Settings UI. Never returns secret values;
        non-secret field values (e.g. the Ollama base URL) ARE returned so the form can prefill.
        """
        out: list[dict[str, Any]] = []
        for d in provider_descriptors():
            profile = self.secrets.get(f"provider:{d.name}") or {}
            configured = descriptor_configured(d, profile)
            values = {
                f.key: profile.get(f.key)
                for f in d.fields
                if not f.secret and profile.get(f.key)
            }
            out.append(
                {
                    **d.to_dict(),
                    "configured": configured,
                    "values": values,
                    "suggested_models": self._suggested_models(d.name),
                    # Key hygiene for the Settings pane: when the key was saved (date, stamped
                    # by set_provider) and when the provider last served a completion (epoch,
                    # stamped by the router's on_use hook). Absent for env-only config.
                    "key_set_at": profile.get("key_set_at"),
                    "last_used_at": (self._prefs.get("provider_last_used") or {}).get(
                        d.name
                    ),
                }
            )
        return out

    def pick_native_folder(self) -> dict[str, Any]:
        """Open the OS folder picker FROM THE SIDECAR — the browser GUI can't obtain absolute
        paths from web file dialogs, but the sidecar is local and can (the desktop shell uses
        Tauri's own picker instead). Blocking until pick/cancel; callers run it off-thread.
        """
        import subprocess
        import sys

        if sys.platform == "darwin":
            cmd = [
                "osascript",
                "-e",
                'tell application "System Events" to activate',
                "-e",
                'POSIX path of (choose folder with prompt "Give the coworker access to a folder")',
            ]
        elif sys.platform == "win32":
            # WinForms folder dialog via PowerShell — no extra deps. -STA is required
            # (the dialog silently fails in the default MTA apartment).
            # Output as UTF-8 so non-ASCII paths (e.g. Chinese folders) survive the
            # subprocess pipe; the default cp1252/codepage decode mangles them and the
            # path arrives empty/garbled in the browser GUI's input field.
            ps = (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$f.Description = 'Give the coworker access to a folder'; "
                "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ [Console]::Out.Write($f.SelectedPath) }"
            )
            cmd = ["powershell.exe", "-NoProfile", "-STA", "-Command", ps]
        else:
            # Linux: zenity when present; otherwise the GUI's paste-a-path input remains.
            cmd = ["zenity", "--file-selection", "--directory"]
        try:
            out = subprocess.run(
                cmd, capture_output=True, timeout=300, encoding="utf-8", errors="replace"
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "error": "no native folder picker available"}
        path = (out.stdout or "").strip()
        if out.returncode != 0 or not path:
            return {"ok": False, "canceled": True}
        return {"ok": True, "path": path}

    def _note_provider_use(self, name: str) -> None:
        """Router on_use hook: remember when a provider last served a completion. Persisted
        THROTTLED (once per provider per minute) — this fires on every model call, from engine
        threads, and prefs.json isn't a place for a write-per-token-of-work."""
        import time

        now = time.time()
        used = self._prefs.setdefault("provider_last_used", {})
        if now - float(used.get(name) or 0) < 60:
            return
        used[name] = now
        try:
            self._save_prefs()
        except OSError:
            pass

    # Suggestions for the OpenAI-compatible vendor providers (checked against vendor docs
    # 2026-07-04; refresh alongside `recommended_model` in providers/registry.py).
    COMPAT_MODELS = {
        "zai": ["glm-5.2", "glm-4.6"],
        "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "kimi": ["kimi-k2.6", "kimi-k2.5"],
        "minimax": ["MiniMax-M2.5", "MiniMax-M2.5-highspeed", "MiniMax-M3"],
        "qwen": ["qwen3-max", "qwen3-coder-plus", "qwen-plus"],
        "xai": ["grok-4.3", "grok-4"],
        "mistral": ["mistral-large-latest", "mistral-small-latest"],
    }

    def _suggested_models(self, name: str) -> list[str]:
        """Bare model-name suggestions for the 'add model' form (datalist), per provider.
        Ollama → live `/api/tags` (best-effort); everyone else → the curated matrix,
        topped up with the compat-vendor extras the matrix doesn't vouch for."""
        if name == "ollama":
            return [m.split(":", 1)[-1] for m in self._ollama_models()]
        from ..providers.matrix import models_for_provider

        return list(
            dict.fromkeys(
                [*models_for_provider(name), *self.COMPAT_MODELS.get(name, [])]
            )
        )

    def set_provider(
        self, name: str, fields: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        """Store a provider's config in its `provider:<name>` SecretStore profile and rebuild
        its cached client. Merges provided fields into any existing profile."""
        d = get_descriptor(name)
        if d is None:
            return {"ok": False, "error": f"unknown provider: {name}"}
        fields = fields or {}
        profile = dict(self.secrets.get(f"provider:{name}") or {})
        for f in d.fields:
            if f.key not in fields:
                continue
            val = fields.get(f.key)
            if isinstance(val, str):
                val = val.strip()
            if val:
                profile[f.key] = val
            elif not f.required:
                profile.pop(f.key, None)
        missing = [f.label for f in d.fields if f.required and not profile.get(f.key)]
        if missing:
            return {"ok": False, "error": "missing: " + ", ".join(missing)}
        # A (re)pasted key stamps its save date — Settings shows "key added <date>" so stale
        # keys are visible. Endpoint-only saves keep the original stamp.
        if isinstance(fields.get("api_key"), str) and fields["api_key"].strip():
            from datetime import date

            profile["key_set_at"] = date.today().isoformat()
        self.secrets.put(f"provider:{name}", profile)
        self._refresh_provider(name)
        # Convenience: if the provider recommends a model and it's actually available, add it to
        # the curated list so it shows up in the composer right after configuring the provider.
        rec = d.recommended_model
        added: Optional[str] = None
        if rec and rec in self._suggested_models(name):
            # OpenAI models stay bare (the router's default); others carry their prefix.
            added = rec if name == "openai" else f"{name}:{rec}"
            self.add_model(added)
        # First working provider wins the default: if the current default model belongs to a
        # provider with no usable config (the fresh-install gpt-5.6-sol case), switch the default to
        # this provider's model. A default that already works is never stolen.
        if added and not self._provider_configured(self._model_provider(self.model)):
            self.set_default_model(added)
        return {"ok": True, "provider": name, "recommended_model": rec}

    def remove_provider(self, name: str) -> dict[str, Any]:
        """Forget a provider's stored config (Settings ▸ Models "Remove key"). The whole
        `provider:<name>` profile goes — key, endpoint, key_set_at — so the provider reads
        as never configured. Curated models stay; they just gray out until a new key."""
        d = get_descriptor(name)
        if d is None:
            return {"ok": False, "error": f"unknown provider: {name}"}
        self.secrets.delete(f"provider:{name}")
        self._refresh_provider(name)
        return {"ok": True, "provider": name}

    def verify_provider(
        self, name: str, fields: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        """Test a provider's credentials with a live read-only call, WITHOUT persisting them, so
        onboarding can offer a "Test" button. Falls back to stored/env values when the form left
        a field blank (e.g. testing an already-configured provider)."""
        import os

        d = get_descriptor(name)
        if d is None:
            return {"ok": False, "error": f"unknown provider: {name}"}
        fields = fields or {}
        profile = self.secrets.get(f"provider:{name}") or {}
        merged = {}
        for f in d.fields:
            val = fields.get(f.key) or profile.get(f.key) or ""
            if isinstance(val, str):
                val = val.strip()
            if val:
                merged[f.key] = val
        api_key = merged.get("api_key", "")
        if not api_key and d.env_key:
            api_key = os.environ.get(d.env_key, "").strip()
        has_key_field = any(f.key == "api_key" for f in d.fields)
        if d.needs_key and has_key_field and not api_key:
            return {"ok": False, "error": "Enter an API key to test."}
        if d.needs_key and not has_key_field:
            # Multi-field cloud providers (Bedrock): required fields must be present;
            # actual credentials may be ambient (~/.aws, env) and are checked by the call.
            missing = [f.label for f in d.fields if f.required and not merged.get(f.key)]
            if missing:
                return {"ok": False, "error": "missing: " + ", ".join(missing)}
        return verify_provider_key(
            name, api_key=api_key, base_url=merged.get("base_url", ""), fields=merged
        )

    def _model_provider(self, model: str) -> str:
        """The provider a model string routes to (known `prefix:` or the OpenAI default)."""
        if ":" in (model or ""):
            prefix = model.split(":", 1)[0]
            if get_descriptor(prefix) is not None:
                return prefix
        return "openai"

    def _provider_configured(self, name: str) -> bool:
        d = get_descriptor(name)
        if d is None:
            return False
        return descriptor_configured(d, self.secrets.get(f"provider:{name}") or {})

    # -- settings / prefs (model API key, default model, onboarding) -------------
    def _prefs_path(self) -> Path:
        return self._data_base / "prefs.json"

    def _load_prefs(self) -> dict[str, Any]:
        try:
            return json.loads(self._prefs_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_prefs(self) -> None:
        self._prefs_path().write_text(
            json.dumps(self._prefs, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # -- direct-message routing -------------------------------------------------
    def dm_session(self) -> Optional[str]:
        """The session a DM to the bot is routed to (user-designated). None → DMs are parked."""
        sid = self._prefs.get("dm_session")
        return sid or None

    def set_dm_session(self, session_id: Optional[str]) -> dict[str, Any]:
        """Designate (or clear, with a falsy id) the session that handles incoming DMs."""
        sid = (session_id or "").strip()
        if sid:
            self._prefs["dm_session"] = sid
        else:
            self._prefs.pop("dm_session", None)
        self._save_prefs()
        return {"ok": True, "dm_session": self.dm_session()}

    def _ollama_alive(self) -> bool:
        """Best-effort local-Ollama liveness, cached 30s (get_settings runs on every GUI
        fetch — no 2s probe inline). Keyless is not the same as PRESENT: `ollama:*` picker
        entries render only when an Ollama actually answers, so a machine with no Ollama
        never shows phantom local models (e.g. a stray pasted string saved as a model id,
        caught 2026-07-21)."""
        import time

        now = time.monotonic()
        cached = getattr(self, "_ollama_alive_cache", None)
        if cached and now - cached[0] < 30:
            return cached[1]
        profile = self.secrets.get("provider:ollama") or {}
        base = (profile.get("base_url") or "http://localhost:11434").strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        try:
            import httpx

            alive = httpx.get(base + "/api/tags", timeout=0.8).status_code == 200
        except Exception:
            alive = False
        self._ollama_alive_cache = (now, alive)
        return alive

    def _ollama_models(self) -> list[str]:
        """Live list of models pulled into the configured Ollama server (via its native
        `/api/tags`), as `ollama:<name>` so they're directly selectable. Empty if Ollama isn't
        configured or unreachable — best-effort, never raises."""
        profile = self.secrets.get("provider:ollama")
        if not profile:
            return []
        base = (profile.get("base_url") or "http://localhost:11434").strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        try:
            import httpx

            data = httpx.get(base + "/api/tags", timeout=2.0).json()
            return [
                f"ollama:{m['name']}" for m in data.get("models", []) if m.get("name")
            ]
        except Exception:
            return []

    def _curated_models(self) -> list[str]:
        """The models offered in the composer's selector: every curated-matrix model
        (`get_settings` culls the ones whose provider has no key) plus custom ids the user
        added, minus matrix models they removed. Deliberately NO built-in seed list — a
        fresh install offers nothing until a provider key exists, and then exactly that
        provider's matrix models appear. The active default is always kept selectable.
        """
        from ..providers.matrix import MATRIX

        user = self._prefs.get("models")
        user = user if isinstance(user, list) else []
        hidden = set(self._prefs.get("hidden_models") or [])
        models = [m for m in [*MATRIX, *user] if m not in hidden]
        return list(dict.fromkeys([self.model, *models]))

    def add_model(self, model: str) -> dict[str, Any]:
        """Add a model id (e.g. `gpt-4o`, `ollama:qwen2.5-coder:32b`) to the picker.
        Custom ids persist in prefs; a previously removed matrix model is just unhidden
        (storing it too would shadow future matrix updates)."""
        from ..providers.matrix import MATRIX

        model = (model or "").strip()
        if not model:
            return {"ok": False, "error": "empty model"}
        hidden = [m for m in self._prefs.get("hidden_models") or [] if m != model]
        if hidden:
            self._prefs["hidden_models"] = hidden
        else:
            self._prefs.pop("hidden_models", None)
        models = self._prefs.get("models")
        models = models if isinstance(models, list) else []
        if model not in models and model not in MATRIX:
            models.append(model)
        self._prefs["models"] = models
        self._save_prefs()
        return {"ok": True, **self.get_settings()}

    def remove_model(self, model: str) -> dict[str, Any]:
        """Remove a model id from the picker. Custom ids are dropped; matrix models are
        hidden by id (the matrix is derived, not stored, so a bare drop would resurrect
        them on the next read)."""
        from ..providers.matrix import MATRIX

        models = self._prefs.get("models")
        models = models if isinstance(models, list) else []
        self._prefs["models"] = [m for m in models if m != model]
        if model in MATRIX:
            hidden = self._prefs.get("hidden_models") or []
            if model not in hidden:
                self._prefs["hidden_models"] = [*hidden, model]
        self._save_prefs()
        return {"ok": True, **self.get_settings()}

    def get_settings(self) -> dict[str, Any]:
        """Model-access + UI status. Never returns the key; `source` says where it comes from."""
        import os

        env_key = bool(os.environ.get("OPENAI_API_KEY"))
        stored = bool((self.secrets.get("provider:openai") or {}).get("api_key"))
        # Only surface models whose provider is actually configured — the composer picker
        # reflects exactly what's connected. The active default is always kept selectable
        # (it's hidden behind the "No model" state until a provider is connected anyway).
        # Ollama is keyless, so "configured" is meaningless there — its models show only
        # while a local Ollama answers (cached liveness probe).
        def _selectable(m: str) -> bool:
            provider = self._model_provider(m)
            if provider == "ollama":
                return self._ollama_alive()
            return self._provider_configured(provider)

        selectable = [m for m in self._curated_models() if _selectable(m)]
        if self.model not in selectable:
            selectable.insert(0, self.model)
        from ..providers.matrix import model_context_windows, model_labels

        return {
            "provider": "openai",
            "model": self.model,
            "models": selectable,
            # Curated-matrix display names ({full id → "GLM-5.2 · via Together"}) so every
            # picker shows human labels; custom models absent here render their raw id.
            "model_labels": model_labels(),
            # {full id → context window in tokens}, verified matrix entries only —
            # drives the composer's context-fill meter (absent id → meter hides).
            "model_context_windows": model_context_windows(),
            "has_key": env_key or stored,
            # Provider-agnostic "can this default model actually run?" — true when the default
            # model's provider is configured (any provider, not just OpenAI). Drives the GUI's
            # "No model connected" composer chip and the onboarding Skip warning.
            "model_ready": self._provider_configured(self._model_provider(self.model)),
            "source": "env" if env_key else ("store" if stored else None),
            "onboarded": bool(self._prefs.get("onboarded")),
            "experimental_connectors": experimental_enabled(self.secrets),
            "surfaces": self._surfaces(),
            "nav_layout": self._nav_layout(),
            "sessions_peek": self.sessions_peek(),
            "context_bar": self.context_bar(),
            "scratch_base": self._prefs.get("scratch_base")
            or self.DEFAULT_SCRATCH_BASE,
            # Real on-disk secrets location, so the UI shows the OS-native path instead of a
            # hardcoded POSIX one (Windows -> %APPDATA%\coworker, macOS/Linux -> ~/.config).
            "secrets_path": str(self.secrets.path),
            **self.pdf_settings(),
            **self.compaction_settings_payload(),
        }

    def _surfaces(self) -> dict[str, bool]:
        """Which session surfaces are shown in the sidebar. Cowork is always on; Chat and Code
        are opt-in (default off) so a new user sees Cowork only."""
        return {
            "cowork": True,
            "chat": bool(self._prefs.get("show_chat", False)),
            "code": bool(self._prefs.get("show_code", False)),
        }

    def set_surfaces(
        self, chat: Optional[bool] = None, code: Optional[bool] = None
    ) -> dict[str, Any]:
        """Toggle Chat/Code visibility (Cowork is always shown). Persisted in prefs."""
        if chat is not None:
            self._prefs["show_chat"] = bool(chat)
        if code is not None:
            self._prefs["show_code"] = bool(code)
        self._save_prefs()
        return {"ok": True, "surfaces": self._surfaces()}

    def _nav_layout(self) -> str:
        """Sidebar layout: ``"flat"`` (default) or ``"grouped"`` (by persona). Persisted in
        prefs (UI-REFRESH §7)."""
        return "grouped" if self._prefs.get("nav_layout") == "grouped" else "flat"

    def set_nav_layout(self, nav_layout: str) -> dict[str, Any]:
        """Set + persist the sidebar layout. Unknown values fall back to ``"flat"``."""
        value = "grouped" if (nav_layout or "").strip() == "grouped" else "flat"
        self._prefs["nav_layout"] = value
        self._save_prefs()
        return {"ok": True, "nav_layout": value}

    DEFAULT_SESSIONS_PEEK = 5

    def sessions_peek(self) -> int:
        """How many sessions a sidebar group shows before "Show more" (owner ask, 2026-07-03)."""
        try:
            n = int(self._prefs.get("sessions_peek", self.DEFAULT_SESSIONS_PEEK))
        except (TypeError, ValueError):
            n = self.DEFAULT_SESSIONS_PEEK
        return max(1, min(n, 50))

    def set_sessions_peek(self, n: int) -> dict[str, Any]:
        try:
            self._prefs["sessions_peek"] = max(1, min(int(n), 50))
        except (TypeError, ValueError):
            return {"ok": False, "error": "sessions_peek must be a number"}
        self._save_prefs()
        return {"ok": True, "sessions_peek": self.sessions_peek()}

    def context_bar(self) -> bool:
        """Whether the composer shows the context-window fill bar. OFF by default (owner
        ask): the chip then states the session total, and the popover keeps both numbers."""
        return bool(self._prefs.get("context_bar", False))

    def set_context_bar(self, shown: Any) -> dict[str, Any]:
        self._prefs["context_bar"] = bool(shown)
        self._save_prefs()
        return {"ok": True, "context_bar": self.context_bar()}

    # -- PDF attachments / token savings (owner ask, 2026-07-17) ----------------
    DEFAULT_PDF_MAX_PAGES = 20
    DEFAULT_PDF_MAX_MB = 10

    def pdf_settings(self) -> dict[str, Any]:
        """Fallback mode for models without native PDF support + the attach-time
        thresholds (Settings → Token savings: big PDFs quietly eat tokens)."""
        from ..pdf_support import FALLBACK_MODES

        mode = self._prefs.get("pdf_fallback")
        try:
            pages = int(self._prefs.get("pdf_max_pages", self.DEFAULT_PDF_MAX_PAGES))
        except (TypeError, ValueError):
            pages = self.DEFAULT_PDF_MAX_PAGES
        try:
            mb = int(self._prefs.get("pdf_max_mb", self.DEFAULT_PDF_MAX_MB))
        except (TypeError, ValueError):
            mb = self.DEFAULT_PDF_MAX_MB
        return {
            "pdf_fallback": mode if mode in FALLBACK_MODES else "text",
            "pdf_max_pages": max(1, min(pages, 100)),
            "pdf_max_mb": max(1, min(mb, 10)),
        }

    def compaction_settings(self) -> dict[str, Any]:
        """The live auto-compaction knobs (OPE-27) — read by every engine per check, so a
        Settings change applies without a rebuild. Only the two spec'd overrides plus the
        summarizer-model pin; absent keys fall back to compaction.py defaults."""
        from ..compaction import DEFAULT_CAP_TOKENS, DEFAULT_THRESHOLD_PCT

        return {
            "threshold_pct": float(
                self._prefs.get("compaction_threshold_pct") or DEFAULT_THRESHOLD_PCT
            ),
            "cap_tokens": int(
                self._prefs.get("compaction_cap_tokens") or DEFAULT_CAP_TOKENS
            ),
            # "" → the session's own model (engine falls back to self.model).
            "model": str(self._prefs.get("compaction_model") or ""),
        }

    def compaction_settings_payload(self) -> dict[str, Any]:
        """The same knobs under REST-facing names (prefixed to keep /v1/settings flat)."""
        settings = self.compaction_settings()
        return {
            "compaction_threshold_pct": settings["threshold_pct"],
            "compaction_cap_tokens": settings["cap_tokens"],
            "compaction_model": settings["model"],
        }

    def set_compaction_settings(
        self,
        threshold_pct: Any = None,
        cap_tokens: Any = None,
        model: Any = None,
    ) -> dict[str, Any]:
        """Persist the auto-compaction overrides (OPE-27). Threshold is a percentage of
        the model's context window (10–95); the cap is an absolute token ceiling; model
        pins the summarizer ('' → the session's own model). Engines read these live via
        `compaction_settings()`, so changes apply to running sessions immediately."""
        if threshold_pct is not None:
            try:
                pct = float(threshold_pct)
            except (TypeError, ValueError):
                return {"ok": False, "error": "compaction_threshold_pct 必须是数字"}
            if not 0.10 <= pct <= 0.95:
                return {
                    "ok": False,
                    "error": "compaction_threshold_pct 必须在 0.10 到 0.95 之间",
                }
            self._prefs["compaction_threshold_pct"] = pct
        if cap_tokens is not None:
            try:
                self._prefs["compaction_cap_tokens"] = max(
                    10_000, min(int(cap_tokens), 2_000_000)
                )
            except (TypeError, ValueError):
                return {"ok": False, "error": "compaction_cap_tokens 必须是数字"}
        if model is not None:
            self._prefs["compaction_model"] = str(model)
        self._save_prefs()
        return {"ok": True, **self.compaction_settings()}

    def set_pdf_settings(
        self,
        fallback: Any = None,
        max_pages: Any = None,
        max_mb: Any = None,
    ) -> dict[str, Any]:
        from ..pdf_support import FALLBACK_MODES, set_fallback_mode

        if fallback is not None:
            if fallback not in FALLBACK_MODES:
                return {"ok": False, "error": "pdf_fallback must be 'text' or 'images'"}
            self._prefs["pdf_fallback"] = fallback
        for key, value, ceiling in (
            ("pdf_max_pages", max_pages, 100),
            ("pdf_max_mb", max_mb, 10),
        ):
            if value is None:
                continue
            try:
                self._prefs[key] = max(1, min(int(value), ceiling))
            except (TypeError, ValueError):
                return {"ok": False, "error": f"{key} must be a number"}
        self._save_prefs()
        settings = self.pdf_settings()
        set_fallback_mode(settings["pdf_fallback"])  # engines read the module global
        return {"ok": True, **settings}

    def set_model_key(self, api_key: str) -> dict[str, Any]:
        """Persist the model API key to the SecretStore (0600). The new provider client is
        built lazily on the next turn, so it picks the key up without a restart."""
        api_key = (api_key or "").strip()
        if not api_key:
            return {"ok": False, "error": "empty api key"}
        # Merge, don't replace: the profile may also hold a custom endpoint (base_url).
        profile = dict(self.secrets.get("provider:openai") or {})
        profile.update({"type": "api_key", "api_key": api_key})
        self.secrets.put("provider:openai", profile)
        self._refresh_provider("openai")  # rebuild the OpenAI client with the new key
        return {"ok": True, **self.get_settings()}

    def set_default_model(self, model: str) -> dict[str, Any]:
        """Set + persist the default model for new sessions (the UI pre-selects it)."""
        model = (model or "").strip()
        if not model:
            return {"ok": False, "error": "empty model"}
        self.model = model
        self._prefs["default_model"] = model
        self._save_prefs()
        return {"ok": True, **self.get_settings()}

    def set_onboarded(self, value: bool = True) -> dict[str, Any]:
        """Record that first-run setup is complete (so it isn't shown again)."""
        self._prefs["onboarded"] = bool(value)
        self._save_prefs()
        return {"ok": True, "onboarded": bool(value)}

    def set_scratch_base(self, path: str) -> dict[str, Any]:
        """Set + persist the common area where each Cowork conversation's scratch directory is
        created (default ~/OpenWorker). The raw value is stored so the UI shows it as entered;
        new conversations use it immediately (existing ones keep their provisioned dir).
        """
        path = (path or "").strip()
        if not path:
            return {"ok": False, "error": "empty path"}
        try:
            Path(path).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        self._prefs["scratch_base"] = path
        self._save_prefs()
        return {"ok": True, **self.get_settings()}

    # -- gateway + connector allow-list (inbound messaging) ---------------------
    def allow_user(
        self,
        name: str,
        user_id: str,
        team_id: Optional[str] = None,
        *,
        display_name: str = "",
    ) -> dict[str, Any]:
        out = self._set_allowed(name, user_id, team_id=team_id, add=True)
        # Directory picks arrive with the name in hand — record it so the chip
        # is readable immediately (message-driven allows learn it on arrival).
        if out.get("ok") and display_name:
            self._note_person(name, user_id, display_name)
        return out

    def disallow_user(
        self, name: str, user_id: str, team_id: Optional[str] = None
    ) -> dict[str, Any]:
        if name == "slack" and user_id in self.slack_approval_owner_ids(team_id):
            return {
                "ok": False,
                "error": "Remove this person as an approval owner first.",
            }
        return self._set_allowed(name, user_id, team_id=team_id, add=False)

    def slack_approval_owner_ids(self, team_id: Optional[str] = None) -> set[str]:
        """Stable Slack user ids allowed to resolve consequential Inbox prompts.

        Managed relay installs are installer-owned. Manual Socket Mode has no
        human OAuth identity, so its owners are selected explicitly.
        """
        key = f"slack:team:{team_id}" if team_id else "slack:default"
        profile = self.secrets.get(key) or {}
        if team_id:
            installer = str(profile.get("slack_user_id") or "").strip()
            return {installer} if installer else set()
        if profile.get("mode") == "relay":
            return set()
        return {
            str(user_id).strip()
            for user_id in (profile.get("approval_owner_ids") or [])
            if str(user_id).strip()
        }

    def set_slack_approval_owner(
        self, user_id: str, *, add: bool, display_name: str = ""
    ) -> dict[str, Any]:
        """Edit Manual Socket Mode approval owners.

        Owner status implies inbound permission. Relay ownership is derived from
        the OAuth installer and is intentionally not editable here.
        """
        user_id = str(user_id).strip()
        if not user_id:
            return {"ok": False, "error": "user_id required"}
        profile = self.secrets.get("slack:default")
        if not profile:
            return {"ok": False, "error": "Slack is not connected in Manual mode."}
        if profile.get("mode") == "relay" or profile.get("managed"):
            return {
                "ok": False,
                "error": "Relay approval ownership is set by the Slack installer.",
            }

        owners = self.slack_approval_owner_ids()
        if add:
            owners.add(user_id)
        else:
            owners.discard(user_id)
            if not owners and self._has_manual_slack_inbox_binding():
                return {
                    "ok": False,
                    "error": (
                        "Choose another approval owner before removing the last one "
                        "while Slack Inbox routing is active."
                    ),
                }
        profile["approval_owner_ids"] = sorted(owners)
        if add:
            allowed = set(profile.get("allowed_users") or [])
            allowed.add(user_id)
            profile["allowed_users"] = sorted(allowed)
        self.secrets.put("slack:default", profile)
        if display_name:
            self._note_person("slack", user_id, display_name)
        if self.gateway is not None and "slack" in self.gateway.settings:
            self.gateway.settings["slack"].allowed_users = set(
                profile.get("allowed_users") or []
            )
        return {
            "ok": True,
            "approval_owner_ids": sorted(owners),
            "allowed_users": list(profile.get("allowed_users") or []),
        }

    def _has_manual_slack_inbox_binding(self) -> bool:
        for raw in self.inbox_routing.bindings():
            if raw.get("channel") != "slack":
                continue
            team_id, _ = slack_split(str(raw.get("target") or ""))
            if team_id is None:
                return True
        return False

    def _slack_actor_owns_item(
        self,
        item,
        *,
        actor_id: str,
        chat_id: str,
        team_id: Optional[str],
    ) -> bool:
        """Authorize a Slack resolution against both its owner and delivery binding."""
        event_team, event_channel = slack_split(chat_id)
        event_team = team_id or event_team
        binding = self.inbox_routing.binding_for(item.inbox)
        owner_team = event_team
        if binding.channel == "slack":
            owner_team, bound_channel = slack_split(binding.target)
            if owner_team != event_team or bound_channel != event_channel:
                return False
        return bool(actor_id) and actor_id in self.slack_approval_owner_ids(owner_team)

    def set_inbox_binding(
        self, name: str, *, channel: Optional[str], target: str
    ) -> dict[str, Any]:
        """Persist an Inbox transport after validating its approval identity."""
        channel = str(channel or "").strip() or None
        target = str(target or "").strip()
        if channel and not target:
            return {"ok": False, "error": "Choose a destination channel."}
        if channel == "slack":
            settings = load_settings(self.secrets).get("slack")
            if settings is None or not settings.enabled:
                return {"ok": False, "error": "Slack is not connected."}
            team_id, destination = slack_split(target)
            if not destination:
                return {"ok": False, "error": "Choose a destination channel."}
            key = f"slack:team:{team_id}" if team_id else "slack:default"
            if not self.secrets.get(key):
                return {
                    "ok": False,
                    "error": "That Slack workspace is not connected.",
                }
            if not self.slack_approval_owner_ids(team_id):
                return {
                    "ok": False,
                    "error": (
                        "Choose at least one approval owner in Slack settings before "
                        "routing Inbox requests there."
                    ),
                }
        self.inbox_routing.set_binding(name, channel=channel, target=target)
        return {"ok": True, "bindings": self.inbox_routing.bindings()}

    def _set_allowed(
        self, name: str, user_id: str, *, team_id: Optional[str] = None, add: bool
    ) -> dict[str, Any]:
        """Add/remove a sender on the allow-list. With `team_id` the edit targets that
        scope's profile — a workspace's `slack:team:<id>`, or a GitHub App
        installation's `github:install:<id>` (the same per-tenant pattern);
        without, the flat `<name>:default` list (manual single-workspace mode)."""
        user_id = str(user_id).strip()
        if not user_id:
            return {"ok": False, "error": "user_id required"}
        if name == "wechat_ilink" and team_id:
            profile_key = f"wechat_ilink:account:{team_id}"
        else:
            scope = "install" if name == "github" else "team"
            profile_key = f"{name}:{scope}:{team_id}" if team_id else f"{name}:default"
        profile = self.secrets.get(profile_key)
        if not profile:
            return {
                "ok": False,
                "error": (
                    "workspace not connected" if team_id else "connector not connected"
                ),
            }
        allowed = set(profile.get("allowed_users") or [])
        allowed.add(user_id) if add else allowed.discard(user_id)
        profile["allowed_users"] = sorted(allowed)
        self.secrets.put(profile_key, profile)
        # reflect into the live gateway so it takes effect without a restart
        if self.gateway is not None and name in self.gateway.settings:
            if team_id:
                from ..connectors import TeamAuth

                teams = self.gateway.settings[name].teams
                team = teams.setdefault(team_id, TeamAuth())
                team.allowed_users = set(allowed)
            else:
                self.gateway.settings[name].allowed_users = set(allowed)
        return {"ok": True, "allowed_users": sorted(allowed), "team_id": team_id}

    async def disconnect_slack_workspace(self, team_id: str) -> dict[str, Any]:
        """Stop relaying ONE workspace: delete the cloud routing row (best-effort),
        drop the local per-team token, and hot-reload the gateway. Removing the last
        workspace also clears relay mode on slack:default so the connector reads
        disconnected (the manual Socket Mode fields, if any, are left untouched)."""
        team_id = str(team_id).strip()
        profile_key = f"slack:team:{team_id}"
        if not team_id or not self.secrets.get(profile_key):
            return {"ok": False, "error": "workspace not connected"}
        from .. import cloud
        from ..config import load_config

        await asyncio.to_thread(
            lambda: cloud.slack_disconnect_workspace(
                self.secrets, load_config(), team_id
            )
        )
        self.secrets.delete(profile_key)
        remaining = [
            m["profile"]
            for m in self.secrets.status()
            if m.get("profile", "").startswith("slack:team:")
        ]
        if not remaining:
            default = self.secrets.get("slack:default") or {}
            if default.get("mode") == "relay":
                default.pop("mode", None)
                default.pop("managed", None)
                if default.get("bot_token"):
                    # Manual Socket Mode creds predating the relay switch: keep them
                    # stored but DISABLED — removing the last workspace must never
                    # silently start listening with old tokens.
                    default["type"] = "token"
                    default["enabled"] = False
                    self.secrets.put("slack:default", default)
                else:
                    default.pop("type", None)
                    default.pop("enabled", None)
                    if default:  # e.g. a flat allow-list worth keeping
                        self.secrets.put("slack:default", default)
                    else:
                        self.secrets.delete("slack:default")
        await self.refresh_gateway()
        return {"ok": True, "remaining_workspaces": len(remaining)}

    def slack_status(self) -> dict[str, Any]:
        """Slack connection health in three honest layers (UX-DECISIONS §21):
        the desktop↔relay socket, the cloud sign-in that authorizes it, and each
        workspace's bot token. The desktop can't see the Slack↔cloud leg, so no
        layer here ever claims it — event silence ≠ outage."""
        from .. import cloud

        default = self.secrets.get("slack:default") or {}
        mode = default.get("mode") or ""
        signin = cloud.status(self.secrets)

        relay: dict[str, Any] = {
            "state": "offline",
            "reconnects": 0,
            "last_event_at": None,
            "last_error": "",
        }
        teams: dict[str, Any] = {}
        adapter = (
            self.gateway._adapters.get("slack") if self.gateway is not None else None
        )
        snapshot = getattr(
            adapter, "status", None
        )  # relay adapter only; Socket Mode has none
        if callable(snapshot):
            relay = snapshot()
            teams = relay.pop("teams", {})
        return {
            "ok": True,
            "mode": mode,
            "relay": relay,
            "signed_in": bool(signin.get("signed_in")),
            "teams": teams,
        }

    async def disconnect_github_installation(
        self, installation_id: str
    ) -> dict[str, Any]:
        """Stop relaying ONE GitHub installation: delete the cloud routing rows
        (best-effort), drop the local profile, hot-reload the gateway. The Slack
        per-workspace disconnect, GitHub flavour — a manual PAT stays untouched."""
        installation_id = str(installation_id).strip()
        from .. import cloud
        from ..config import load_config
        from ..connectors import github_installs

        if not installation_id or not self.secrets.get(
            github_installs.PREFIX + installation_id
        ):
            return {"ok": False, "error": "installation not connected"}
        await asyncio.to_thread(
            lambda: cloud.github_disconnect_installation(
                self.secrets, load_config(), installation_id
            )
        )
        result = github_installs.disconnect_install(self.secrets, installation_id)
        await self.refresh_gateway()
        return result

    def github_status(self) -> dict[str, Any]:
        """GitHub relay health, same three honest layers as Slack: the shared
        relay socket, the cloud sign-in, and per-installation token health."""
        from .. import cloud

        default = self.secrets.get("github:default") or {}
        signin = cloud.status(self.secrets)
        relay: dict[str, Any] = {
            "state": "offline",
            "reconnects": 0,
            "last_event_at": None,
            "last_error": "",
        }
        installs: dict[str, Any] = {}
        missed: dict[str, Any] = {}
        adapter = (
            self.gateway._adapters.get("github") if self.gateway is not None else None
        )
        snapshot = getattr(adapter, "status", None)
        if callable(snapshot):
            relay = snapshot()
            installs = relay.pop("installs", {})
            missed = relay.pop("missed", {})
        return {
            "ok": True,
            "mode": default.get("mode") or "",
            "relay": relay,
            "signed_in": bool(signin.get("signed_in")),
            "installs": installs,
            "missed": missed,
        }

    async def start_gateway(self) -> list[str]:
        """Build the messaging gateway and start enabled listeners. Inbound messages route to
        durable sessions: a channel message to its subscribers, a DM to the designated DM session
        (else parked). Returns the platforms whose listeners came up."""
        self.scheduler.start()  # tick scheduler for automations (independent of connectors)
        return await self._build_and_start_gateway()

    async def refresh_gateway(self) -> list[str]:
        """Hot-reload the messaging listeners with fresh secrets — called after a connector
        connect/disconnect so pasting new tokens takes effect immediately. A platform socket
        (Slack Socket Mode) authenticates at connect time, so new creds mean reopening that
        socket; this replaces the adapters in-process — the sidecar never restarts."""
        await self.stop_gateway()
        started = await self._build_and_start_gateway()
        print(f"[coworker] messaging gateway reloaded: {', '.join(started) or 'none'}")
        return started

    async def _build_and_start_gateway(self) -> list[str]:
        self._gateway_loop = asyncio.get_running_loop()
        settings = load_settings(self.secrets)
        self.gateway = Gateway(
            secrets=self.secrets,
            settings=settings,
            handler=self._dispatch_inbound,
            reply_resolver=self._resolve_inbox_reply,
            interaction_handler=self._on_interaction,
            on_unauthorized=self._park_unauthorized,
        )
        # Managed Slack relay wiring (only used when a connector picks relay mode):
        # the cloud sign-in JWT authorizes the relay WebSocket, and the relay
        # endpoint comes from config. Both are lazy — Socket Mode needs neither.
        from ..cloud import fresh_access_token
        from ..config import load_config

        cloud_config = load_config()

        def _relay_token() -> str:
            return fresh_access_token(self.secrets, cloud_config) or ""

        # Every relay-mode platform shares ONE cloud socket; the hub fans frames
        # out by provider tag. Built lazily on the first relay adapter.
        relay_ws_url = getattr(cloud_config, "cloud_relay_ws_url", "") or None
        relay_hub = None
        if relay_ws_url:
            from ..connectors.relay_client import RelayHub

            relay_hub = RelayHub(relay_ws_url, _relay_token)

        async def _github_token(installation_id: str) -> str:
            from ..cloud import github_installation_token

            return await asyncio.to_thread(
                github_installation_token, self.secrets, cloud_config, installation_id
            )

        for platform, st in settings.items():
            if not st.enabled:
                continue
            profile = self.secrets.get(f"{platform}:default") or {}
            adapter = make_adapter(
                platform,
                profile,
                secrets=self.secrets,
                token_provider=_relay_token,
                relay_url=relay_ws_url,
                relay_hub=relay_hub,
                github_token_client=_github_token,
            )
            if adapter is not None:
                self.gateway.register(adapter)
        return await self.gateway.start()

    async def stop_gateway(self) -> None:
        if self.gateway is not None:
            await self.gateway.stop()
            self.gateway = None
        self._gateway_loop = None

    def _live_delivery(self, target: str, text: str) -> SendResult:
        """Bridge a sync tool call in a worker thread onto the live Gateway loop."""
        gateway = self.gateway
        loop = self._gateway_loop
        if gateway is None or loop is None or loop.is_closed() or not loop.is_running():
            return SendResult(False, error="live messaging gateway is unavailable")
        try:
            future = asyncio.run_coroutine_threadsafe(gateway.deliver(target, text), loop)
            return future.result(timeout=55.0)
        except TimeoutError:
            future.cancel()
            return SendResult(False, error="live messaging delivery timed out")
        except Exception:
            return SendResult(False, error="live messaging delivery failed")

    # -- unauthorized inbound (parked, §19) --------------------------------------
    def _note_person(
        self, platform: str, user_id: Optional[str], name: Optional[str]
    ) -> None:
        """Remember a sender's display name (persisted) so ID-keyed surfaces — the allow-list
        chips above all — can show who a U07JK… actually is. Best-effort, newest name wins.
        """
        if not user_id or not name:
            return
        key = f"{platform}:{user_id}"
        if self._people.get(key) != name:
            self._people[key] = name
            try:
                self._people_path.write_text(json.dumps(self._people, ensure_ascii=False))
            except OSError:
                pass

    async def _park_unauthorized(self, event) -> None:
        """Gateway callback: keep what an unallowed sender said (names already resolved by the
        adapter, best-effort) so the owner can allow-and-deliver without a re-send."""
        s = event.source
        self._note_person(s.platform, s.user_id, s.user_name)
        self.parked.park(
            platform=s.platform,
            chat_id=s.chat_id,
            chat_name=s.chat_name,
            user_id=s.user_id or "?",
            user_name=s.user_name,
            chat_type=s.chat_type,
            thread_id=s.thread_id,
            team_id=s.team_id,
            text=event.text or "",
        )

    async def resolve_unauthorized(
        self, name: str, item_id: str, action: str
    ) -> dict[str, Any]:
        """Resolve one parked message: "dismiss" throws it away; "allow" adds the sender to the
        allow-list (future messages flow); "allow_deliver" also re-injects the parked message
        through the NORMAL inbound path — buffer + subscriptions — as if it just arrived.
        """
        item = self.parked.pop(item_id)
        if item is None or item.platform != name:
            return {"ok": False, "error": "unknown item"}
        if action == "dismiss":
            return {"ok": True}
        if action not in ("allow", "allow_deliver"):
            return {"ok": False, "error": f"unknown action: {action}"}
        allowed = self._set_allowed(name, item.user_id, team_id=item.team_id, add=True)
        if not allowed.get("ok"):
            return allowed
        if action == "allow_deliver":
            from ..connectors import MessageEvent, SessionSource

            event = MessageEvent(
                text=item.text,
                source=SessionSource(
                    platform=item.platform,
                    chat_id=item.chat_id,
                    user_id=item.user_id,
                    user_name=item.user_name,
                    chat_name=item.chat_name,
                    chat_type=item.chat_type,
                    thread_id=item.thread_id,
                    team_id=item.team_id,
                ),
            )
            await self._dispatch_inbound(event)
        return {"ok": True}

    # -- per-session live view --------------------------------------------------
    def register_event_client(self, send_cb: Any) -> None:
        self._event_clients.add(send_cb)

    def unregister_event_client(self, send_cb: Any) -> None:
        self._event_clients.discard(send_cb)

    async def broadcast_event(self, message: dict) -> None:
        """Fan an app-wide event out to every /ws/events socket. Best-effort: a dead
        socket is dropped, never fatal to the caller."""
        for cb in list(self._event_clients):
            try:
                await cb(message)
            except Exception:
                self.unregister_event_client(cb)

    def register_session_client(self, session_id: str, send_cb: Any) -> None:
        self._session_clients.setdefault(session_id, set()).add(send_cb)

    def unregister_session_client(self, session_id: str, send_cb: Any) -> None:
        clients = self._session_clients.get(session_id)
        if clients is not None:
            clients.discard(send_cb)
            if not clients:
                self._session_clients.pop(session_id, None)

    async def broadcast_session(self, session_id: str, message: dict) -> None:
        """Fan a turn event out to every socket viewing this session. Best-effort: a dead socket
        is dropped, never fatal to the turn (delivery is socket-independent)."""
        for cb in list(self._session_clients.get(session_id, ())):
            try:
                await cb(message)
            except Exception:
                self.unregister_session_client(session_id, cb)

    async def aclose(self) -> None:
        await self.scheduler.stop()
        await self.wechat_ilink_qr.aclose()
        await self.stop_gateway()
        await self.mcp.aclose()
        self.audit_store.close()

    # -- automation (scheduled tasks) -------------------------------------------
    def approval_prompt_data(self, session_id: str, request) -> dict[str, Any]:
        """Extra Inbox-item payload for a parked approval. Always carries the tool name +
        arguments so the GUI can render the same humanized card (§35) it shows live —
        without them a reopened session fell back to the raw 'Run `tool`?' treatment.
        Automation runs additionally carry the owning task + (when the call is eligible)
        the exact target a standing rule would pin: the GUI offers "Allow every time" only
        when both are present — in-app only, never on Slack-mirrored buttons (§25)."""
        from ..permissions import standing_rule_candidate

        data: dict[str, Any] = {
            "tool": request.tool_name,
            "arguments": getattr(request, "arguments", None) or {},
        }
        task = self.task_store.task_for_run_session(session_id)
        if task is None:
            return data
        data.update({"task_id": task.id, "task_title": task.title})
        target = standing_rule_candidate(
            request.tool_name,
            getattr(request, "arguments", None) or {},
            getattr(request, "metadata", None),
        )
        if target:
            data["standing_target"] = target
        return data

    def mint_task_rule(
        self, session_id: str, tool_name: str, arguments: Any, metadata: Any = None
    ) -> bool:
        """Persist a standing rule a human minted via "Allow every time" on a run's
        approval card (§25's retrofit path). Server-side validation, not trust in the
        card: the session must be an automation run and the call must be rule-eligible
        (external risk, declared target argument, non-empty target). Also applies the
        rule to the live engine so the run's next call auto-allows."""
        from ..permissions import standing_rule_candidate

        task = self.task_store.task_for_run_session(session_id)
        if task is None:
            return False
        target = standing_rule_candidate(tool_name, arguments or {}, metadata)
        if not target or not task.add_rule(tool_name, target):
            return False
        self.task_store.save(task)
        engine = self._engines.get(session_id)
        if engine is not None:
            engine.permissions.task_rules.setdefault(tool_name, set()).add(target)
        try:
            self.audit_store.append(
                {
                    "session_id": session_id,
                    "tool": tool_name,
                    "arguments": arguments or {},
                    "stage": "standing_rule_minted",
                    "status": "granted",
                    "reason": f"allow every time: {tool_name} → {target} (task {task.id})",
                }
            )
        except Exception:
            pass
        return True

    def approval_outcome(self, resolution: str, request, session_id: str):
        """Map an approval resolution (from any surface) to an ApprovalOutcome, handling
        the task-persistent "always_task" vocabulary alongside the session-scoped ones.
        """
        from ..engine import ApprovalOutcome

        if resolution == "always_task":
            self.mint_task_rule(
                session_id,
                request.tool_name,
                getattr(request, "arguments", None),
                getattr(request, "metadata", None),
            )
            return ApprovalOutcome.ONCE
        try:
            return ApprovalOutcome(resolution)
        except ValueError:
            pass
        if resolution == "allow":
            return ApprovalOutcome.ONCE
        if resolution == "always":
            return ApprovalOutcome.ALWAYS_TOOL
        return ApprovalOutcome.DENY

    def _scheduled_approver(self, task, session_id: str):
        from ..engine import ApprovalOutcome
        from ..permissions import WRITE_TOOLS

        name_allowed = task.name_allowed_tools()

        async def approver(request):
            # Unattended: auto-allow the deliverable writes (path-scoped to the task
            # workspace) + tools the task allows BY NAME (legacy entries). Target-bound
            # rules never reach here — the permission engine matched them already.
            if request.tool_name in WRITE_TOOLS or request.tool_name in name_allowed:
                return ApprovalOutcome.ONCE
            # Anything else parks in the Inbox and suspends the run (§25 graceful
            # degradation — an ungranted automation still works, it just asks). The item
            # carries the task binding so the in-app card can offer "Allow every time";
            # the Slack mirror renders only Approve/Deny buttons.
            item = self.inbox.add_approval(
                session_id,
                f"运行 `{request.tool_name}`？",
                body=_approval_body(request),
                inbox=self.inbox_routing.route_for(session_id, task.agent),
                tool_call_id=getattr(request, "tool_call_id", None),
                data=self.approval_prompt_data(session_id, request),
            )
            if item.state == "pending":
                self.persist_session(session_id)
                await self.mirror_inbox_item(item)
            resolution = await self.inbox.wait(item.id)
            return self.approval_outcome(resolution, request, session_id)

        return approver

    def _seed_task_permissions(self, engine: TurnEngine, task) -> None:
        """Apply a task's standing allowances to an engine: target-bound rules feed the
        permission engine's matcher (connector tools included — the target binding is the
        safety); name-only legacy entries keep their session-allowlist behavior."""
        engine.permissions.task_rules = task.standing_rules()
        for tool in task.name_allowed_tools():
            engine.permissions.allow_tool_for_session(tool)

    def _build_task_engine(self, task, *, session_id: str) -> TurnEngine:
        ag = get_agent(task.agent)
        Path(task.workspace).mkdir(parents=True, exist_ok=True)
        engine = build_engine(
            agent=ag,
            workspace=task.workspace,
            model=task.model or self.model,
            mode=Mode.INTERACTIVE,
            approver=self._scheduled_approver(task, session_id),
            provider=self.provider,
            memory_store=self.memory_store,
            secrets=self.secrets,
            # No scheduling tools inside a scheduled run: the executing agent's job is to DO the
            # task, and instructions that mention timing ("every day at 5:32pm…") otherwise tempt
            # it to create another automation instead of running this one.
            task_store=None,
            session_id=session_id,
            audit_sink=self.audit_store.append,
            # Scheduled runs respect the same per-session connection hierarchy as live sessions:
            # expose only the persona's effective-enabled connectors' tools (§4.3).
            connector_filter=self.effective_connectors(session_id, task.agent),
            skill_filter=lambda sid=session_id, w=task.workspace: (
                self.effective_skill_names(sid, w)
            ),
            # User-facing permission rules (E2): allow/deny/ask takes precedence over
            # risk classification in the permission engine.
            rule_resolver=self.rule_store.resolver(),
            # Persona delegation (E3) + slash commands (E3).
            persona_registry=self.personas,
            command_loader=self._engine_command_loader(),
            live_delivery=self._live_delivery,
            # Tool-level hooks (pre_tool/post_tool/on_message) — the same HookStore that
            # fires pre_run/post_run. Firer is None when no tool-event hooks are
            # registered, so the hot path skips the subprocess call entirely.
            tool_hook_firer=self._tool_hook_firer(),
        )
        self._seed_task_permissions(engine, task)
        return engine

    # -- mirroring inbox items to a bound channel -------------------------------
    async def mirror_inbox_item(self, item) -> None:
        """Mirror an Inbox item to its bound channel. Discrete choices (approve/deny, ask_user
        options) render as BUTTONS — the item id rides in each, so a click resolves it
        unambiguously. Free-text answers aren't offered over messaging (open the app).

        Independent of the messaging-channel mirror: a pending decision item (approval/plan/
        directory) from an automation run also fans out through the NotifyRouter so a user who
        stepped away — and who may have NO messaging connector bound, only notify channels like
        钉钉/飞书/企微 — still gets told "your automation needs you." The two paths coexist:
        the binding mirror offers one-tap resolution; the notify ping just raises the alarm.
        """
        from ..interactions import buttons_for

        # Notify-channel fan-out first (best-effort, never blocks the binding mirror).
        await self._notify_inbox_action_needed(item)

        binding = self.inbox_routing.binding_for(item.inbox)
        if not (binding.channel and self.gateway is not None):
            return
        if binding.channel == "slack":
            team_id, _ = slack_split(binding.target)
            # Legacy bindings may predate approval ownership. Keep the item
            # available in-app, but never mirror it to an ownerless channel.
            if not self.slack_approval_owner_ids(team_id):
                return
        target = f"{binding.channel}:{binding.target}"
        body = "\n".join(p for p in (item.title, item.body) if p).strip()
        buttons = buttons_for(item)
        try:
            if buttons:
                await self.gateway.deliver_interactive(target, body, buttons)
            else:
                await self.gateway.deliver(
                    target,
                    f"{body}\n(Open the app to respond.)\n[ow:{item.id}]".strip(),
                )
        except Exception:
            pass

    # -- interactive prompt buttons (Slack/Telegram) ----------------------------
    async def _on_interaction(self, event) -> None:
        """A button click on a mirrored Inbox prompt. The button value carries the item id + the
        resolution, so this is unambiguous — resolve the item, then swap the buttons for the
        outcome. Resolving releases any agent suspended on it (first-responder-wins)."""
        from ..interactions import decode

        decoded = decode(getattr(event, "value", "") or "")
        if decoded is None:
            return
        item_id, resolution = decoded
        item = self.inbox.get(item_id)
        if item is None:
            return
        protected_kinds = {"approval", "directory", "plan"}
        if (
            getattr(event, "platform", "") == "slack"
            and item.kind in protected_kinds
        ):
            actor_id = str(getattr(event, "user_id", "") or "")
            if not self._slack_actor_owns_item(
                item,
                actor_id=actor_id,
                chat_id=getattr(event, "chat_id", "") or "",
                team_id=getattr(event, "team_id", None),
            ):
                if self.gateway is not None:
                    await self.gateway.reject_interaction(event)
                return
        already = item is not None and item.state != "pending"
        resolved = await self.resolve_inbox(item_id, resolution)
        if not resolved and not already:
            return
        who = getattr(event, "user_name", None) or "someone"
        title = item.title
        outcome = "already resolved" if already else f"“{resolution}” — by {who}"
        if self.gateway is not None and getattr(event, "message_id", None):
            try:
                await self.gateway.update_message(
                    getattr(event, "platform", "slack"),
                    getattr(event, "chat_id", ""),
                    event.message_id,
                    f"{title}\n✅ {outcome}",
                )
            except Exception:
                pass

    # -- inbox replies over messaging connectors --------------------------------
    def _resolve_inbox_reply(self, event) -> bool:
        """Try to handle an inbound Slack/Telegram message as an Inbox reply. Returns True if the
        message carried an `[ow:<id>]` token (so it's consumed here, not routed as a new turn) —
        resolving the item also releases any agent suspended on it."""
        from ..inbox_routing import resolve_from_reply

        text = getattr(event, "text", "") or ""

        def _resolve(item_id: str, resolution: str) -> bool:
            item = self.inbox.get(item_id)
            if item is None:
                return False
            if (
                getattr(event.source, "platform", "") == "slack"
                and item.kind in {"approval", "directory", "plan"}
            ):
                actor_id = str(getattr(event.source, "user_id", "") or "")
                if not self._slack_actor_owns_item(
                    item,
                    actor_id=actor_id,
                    chat_id=getattr(event.source, "chat_id", "") or "",
                    team_id=getattr(event.source, "team_id", None),
                ):
                    return False
            return self.inbox.resolve(item_id, resolution)

        return resolve_from_reply(text, _resolve) is not None

    # -- self-wake resumption ---------------------------------------------------
    async def resume_due_wakes(self) -> int:
        """Resume sessions whose self-wakes are due (called each scheduler tick). A suspended
        agent (it called sleep_for / wake_on / wake_on_event and ended its turn) is re-invoked on
        its own session with a wake message so it continues where it left off. Returns the count.
        """
        resumed = 0
        for wake in self.wakes.due():
            try:
                await self._resume_wake(wake)
                resumed += 1
            except Exception:
                pass
            finally:
                self.wakes.mark_fired(wake.id)
        return resumed

    def mark_running(self, session_id: str) -> None:
        self._running_sessions.add(session_id)

    def try_mark_running(self, session_id: str) -> bool:
        """Atomically claim an idle session for one turn on the server event loop."""
        if session_id in self._running_sessions:
            return False
        self._running_sessions.add(session_id)
        return True

    def mark_idle(self, session_id: str) -> None:
        self._running_sessions.discard(session_id)
        # Every turn path (WS, background delivery, durable resume) marks idle when it
        # finishes — the one shared post-turn moment, so auto-titling hooks in here and
        # can never add latency to the response itself.
        self._maybe_autotitle(session_id)

    def is_running(self, session_id: str) -> bool:
        return session_id in self._running_sessions

    async def _resume_wake(self, wake) -> None:
        await self.deliver_to_session(wake.session_id, self._wake_message(wake))

    async def deliver_to_session(
        self, session_id: str, message: str, *, source: Optional[dict[str, Any]] = None,
        reply_target: str = "",
    ) -> None:
        """Deliver an out-of-band message to a (durable) session — the agent stays resumable
        forever, so this works with no live socket. Busy (mid tool-loop): steer it into the live
        turn at its next step (don't start a colliding run). Idle: run a fresh background turn
        (results persist; if the session is Unattended, any approvals route to the Inbox). Shared
        by self-wake and channel-subscription delivery. `source` is the display-only MessageSource
        sidecar for connector messages (framed `message` stays the model-facing text).

        `reply_target` is the exact send_message target a reply should land on (e.g.
        ``wechat_ilink:<account>/<user>`` for a personal-WeChat DM). When set, a task-scoped
        standing rule is seeded so the agent's reply to THAT contact is pre-approved — a DM that
        just arrived should not park its own answer behind a per-call approval prompt (the user
        already opted into the conversation by designating this session as the DM route). The
        rule is target-pinned, so it never covers a different contact or platform.
        """
        engine = self.get_engine(session_id)
        if engine is None:
            return
        # Seed a target-pinned standing allowance so the reply doesn't wait on an approval
        # prompt (which a background/DM turn has no live user watching to grant). Same mechanism
        # as the mention-thread grant; exact-target binding is what keeps it safe.
        if reply_target:
            engine.permissions.task_rules.setdefault("send_message", set()).add(reply_target)
            # Per-turn directive, prepended to the delivered message: the static system-prompt
            # guidance is necessary but not always sufficient — models default to answering in
            # plain text and skip the tool call unless the instruction is co-located with the
            # inbound message. This block sits next to the user's text so the model can't miss it.
            message = (
                f"{message}\n\n"
                f"[system] The sender above is on a messaging channel and cannot see your "
                f"assistant text in this app. You MUST reply by calling the send_message tool "
                f'with target "{reply_target}" and your answer as the text. Do not answer only '
                f"in this conversation — that reaches no one. Call send_message now."
            )
        if not self.try_mark_running(session_id):
            engine.queue_steering(message, source)
            return
        try:
            async for event in engine.run(message, source=source):
                # Stream every event to any socket viewing this session, so a background turn
                # (channel delivery, self-wake, durable resume) is seen live — not just on reselect.
                await self.broadcast_session(
                    session_id, {"type": event.type.value, "data": event.data}
                )
                # A background turn has no user watching to read an inline error: a dead model or
                # tool failure would otherwise vanish. Log it and park it in the dead-letter store.
                if event.type.value == "error":
                    reason = (event.data or {}).get("error", "unknown error")
                    logger.warning(
                        "background turn failed for %s: %s", session_id, reason
                    )
                    self.unrouted.record(session_id, "-", message, reason=reason)
            self.save(session_id, engine)
        except (
            Exception
        ) as exc:  # an unexpected raise out of the turn must not be swallowed
            logger.warning("background turn crashed for %s: %s", session_id, exc)
            self.unrouted.record(session_id, "-", message, reason=str(exc))
            await self.broadcast_session(
                session_id, {"type": "error", "data": {"error": str(exc)}}
            )
        finally:
            self.mark_idle(session_id)
            await self.broadcast_session(session_id, {"type": "turn_done", "data": {}})

    # -- channel subscriptions (inbound messaging) ------------------------------
    async def _dispatch_inbound(self, event) -> None:
        """Route a non-token inbound message. Channel messages are buffered (for catch-up) and
        fanned out to every subscribed session; a DM (or any non-channel) goes to the user-designated
        DM session (delivered like any background turn) or, if none is set, is parked as unrouted.
        """
        src = event.source
        text = getattr(event, "text", "") or ""
        who = src.user_name or src.user_id or "?"
        channel = f"{src.platform}:{src.chat_id}"  # thread-agnostic channel address
        self._note_person(src.platform, src.user_id, src.user_name)
        # Structured sidecar (display-only) built from the resolved identities on the event — the
        # framed text below stays the model-facing `content`; `ms.text` carries the RAW message.
        ms = MessageSource(
            connector=src.platform,
            kind="channel" if src.chat_type in ("channel", "group") else "dm",
            channel_id=src.chat_id,
            channel_name=src.chat_name or src.chat_id,
            sender_id=src.user_id or "",
            sender_name=src.user_name or src.user_id or "?",
            ts=_inbound_epoch(getattr(event, "message_id", None)),
            text=text,
        )
        if src.chat_type in ("channel", "group"):
            self.channel_buffer.record(
                channel, who, text, name=src.chat_name
            )  # buffer all, even unsubscribed
            subs = self.subscriptions.for_channel(channel)
            # §31 mention router: a direct @-mention of the bot outranks the passive fan-out —
            # subscribed sessions must answer it; an unsubscribed channel spawns (or steers)
            # the per-thread coworker session.
            if getattr(event, "mentions_me", False):
                await self._route_mention(event, ms, subs)
                return
            if subs:
                # Chattiness tiers (§31): untagged channel traffic is judgement-only —
                # silence is the default; the must-respond framing is the mention path's.
                msg = (
                    f"💬 New message on {src.chat_name or channel} from {who}: {text}\n"
                    f"(You're subscribed to this channel but were NOT mentioned. Use your "
                    f"judgement: stay silent unless the message clearly concerns your job and "
                    f"a reply adds real value — most channel chatter needs no response from "
                    f'you. If you do reply, use the send_message tool with target "{channel}".)'
                )
                for sub in subs:
                    # Per-session connection hierarchy (§4.3): a session that has muted this
                    # connector skips delivery — the message is still buffered (above) for catch-up.
                    if not self._inbound_connector_allowed(
                        sub.session_id, src.platform
                    ):
                        continue
                    try:
                        await self.deliver_to_session(
                            sub.session_id, msg, source=ms.to_dict()
                        )
                    except Exception:
                        pass
                return
            return  # channel with no subscribers — nobody is listening
        # DM (or any non-channel): route to the designated session, else park it for visibility.
        dm = self.dm_session()
        if dm and self._inbound_connector_allowed(dm, src.platform):
            await self.deliver_to_session(
                dm, event.tagged_text(), source=ms.to_dict(), reply_target=src.target
            )
        elif dm:
            # Designated, but this session has muted the connector → park rather than deliver.
            self.unrouted.record(
                src.target, who, text, reason="connector muted for DM session"
            )
        else:
            self.unrouted.record(
                src.target, who, text, reason="no DM session designated"
            )

    # -- mention router (§31) ----------------------------------------------------
    async def _route_mention(self, event, ms: MessageSource, subs) -> None:
        """@OpenWorker tagged in a channel. A subscribed (user-connected) coworker owns the channel
        and must answer; otherwise the per-thread coworker session handles it — spawned on the
        first tag, steered by follow-ups (deduped on the thread target)."""
        from ..connectors.base import format_target

        src = event.source
        # Slack semantics: replying to a top-level message threads on THAT message's ts, so a
        # top-level tag (no thread_ts) keys — and is answered — on its own ts.
        thread_key = src.thread_id or getattr(event, "message_id", None)
        thread_target = format_target(src.platform, src.chat_id, thread_key)
        who = src.user_name or src.user_id or "?"
        chan = f"#{src.chat_name}" if src.chat_name else src.chat_id
        if subs:
            # The user connected a coworker to this channel — it answers tags; no spawn.
            msg = (
                f"🔔 You were tagged by {who} in {chan}: {event.text}\n"
                f"(You are subscribed to this channel and were mentioned directly — you must "
                f"respond. Reply in the thread with the send_message tool, target "
                f'"{thread_target}".)'
            )
            for sub in subs:
                if not self._inbound_connector_allowed(sub.session_id, src.platform):
                    continue
                try:
                    await self.deliver_to_session(
                        sub.session_id, msg, source=ms.to_dict()
                    )
                except Exception:
                    pass
            return
        sid = self.mention_sessions.get(thread_target)
        if sid and self.session_store.load(sid) is not None:
            # Follow-up tag in a thread we already own → steer the same session.
            msg = (
                f"💬 Follow-up in your Slack thread ({chan}) from {who}: {event.text}\n"
                f'(Reply in the thread with the send_message tool, target "{thread_target}" '
                f"— replies there are pre-approved.)"
            )
            await self.deliver_to_session(sid, msg, source=ms.to_dict())
            return
        await self._spawn_mention_session(event, ms, thread_target)

    async def _spawn_mention_session(
        self, event, ms: MessageSource, thread_target: str
    ) -> None:
        """First tag in a thread: a NEW visible coworker session that owns the thread. Its
        in-thread replies carry a standing grant (§25 shape, exact-target match) so the
        conversation never stalls on an approval nobody in Slack can see; everything else
        asks as usual (approvals park to the Inbox)."""
        import uuid

        src = event.source
        who = src.user_name or src.user_id or "?"
        chan = f"#{src.chat_name}" if src.chat_name else src.chat_id
        sid = uuid.uuid4().hex
        engine = self.get_engine(sid, agent=self.personas.default_id())
        if engine is None:
            self.unrouted.record(
                src.target, who, event.text, reason="could not spawn mention session"
            )
            return
        # Durable mapping FIRST (a fast follow-up tag mid-turn dedupes into steering),
        # then the live grant; get_engine re-derives it from the store on any rebuild.
        self.mention_sessions.set(
            thread_target, sid, channel=f"{src.platform}:{src.chat_id}"
        )
        engine.permissions.task_rules.setdefault("send_message", set()).add(
            thread_target
        )
        self.save(sid, engine)  # the sessions row must exist before rename/set_origin
        # Title = the ASK first, channel last (owner call 2026-07-14): the text is what
        # varies between sessions, so it gets the truncation budget; the mention token is
        # noise (origin is already told by the From Slack group + icon + origin_label).
        ask = re.sub(r"<@[^>]+>", "", event.text or "")
        ask = " ".join(ask.split())[:48]
        self.session_store.rename(sid, f"{ask} — {chan}" if ask else chan)
        label = chan + (f" · {src.team_id}" if src.team_id else "")
        self.session_store.set_origin(sid, src.platform, label)
        # Up to 6 lines of channel context, minus the tag itself (it's the opening line).
        recent = self.channel_buffer.recent(f"{src.platform}:{src.chat_id}", 7)[:-1]
        context = "\n".join(f"- {m['from']}: {m['text']}" for m in recent)
        opening = (
            f"🔔 You were mentioned on Slack in {chan} by {who}: {event.text}\n\n"
            f"You own this Slack thread. Reply in the thread using the send_message tool "
            f'with target "{thread_target}" — replies to this thread are pre-approved and '
            f"never prompt the user. Anything else (other channels, files, external "
            f"actions) asks for approval as usual. Keep replies concise and "
            f"Slack-appropriate."
            + (f"\n\nRecent channel context:\n{context}" if context else "")
        )
        try:
            await self.deliver_to_session(sid, opening, source=ms.to_dict())
        except Exception:
            logger.exception("mention session %s opening turn failed", sid)

    @staticmethod
    def _wake_message(wake) -> str:
        note = f" (note: {wake.note})" if getattr(wake, "note", "") else ""
        if wake.kind == "completion":
            return (
                f"⏰ Wake — the job `{wake.job_id}` you were waiting on has completed{note}. "
                "Continue where you left off."
            )
        if wake.kind == "event":
            return (
                f"⏰ Wake — the event `{wake.event_key}` you were waiting on has fired{note}. "
                "Continue where you left off."
            )
        return (
            f"⏰ Wake — the timer you set has fired{note}. Continue where you left off."
        )

    async def _run_scheduled_task(self, task, trigger: str) -> TaskRun:
        run = TaskRun(
            task_id=task.id, trigger=trigger
        )  # __post_init__ sets run.session_id
        self.task_store.add_run(run)  # mark "running"
        # UX-026: tell every open app window a SCHEDULED run just started (the 5s
        # top-right toast). Manual runs never come through here — the user is
        # already watching those live.
        await self.broadcast_event(
            {
                "type": "automation_run_started",
                "data": {
                    "task_id": task.id,
                    "task_title": task.title,
                    "session_id": run.session_id,
                    "workspace": task.workspace,
                    "agent": task.agent,
                    "trigger": trigger,
                },
            }
        )
        # Each run is a real, persisted conversation thread: it runs the instructions under its
        # own session id, then saves the transcript. The user can reopen that session and ask a
        # follow-up — the scheduled agent is no longer fire-and-forget.
        # pre_run hooks (E2): fire before the engine is built. A hook may request a skip by
        # writing {"skip": true} to stdout — if any hook does, abort the run as "skipped".
        hook_ctx = {
            "event": "pre_run",
            "task_id": task.id,
            "task_name": task.title,
            "session_id": run.session_id,
            "workspace": task.workspace,
            "agent": task.agent,
            "trigger": trigger,
        }
        pre_results = self.hooks.fire("pre_run", hook_ctx)
        if any(r.get("skip") for r in pre_results):
            run.status, run.error = "skipped", "aborted by pre_run hook"
            run.finished_at = _epoch()
            self.task_store.add_run(run)
            return run
        engine = self._build_task_engine(task, session_id=run.session_id)
        # Register the live engine up-front: a parked approval persists the session
        # mid-run (durable suspend), and resolving from the Inbox must find this engine.
        self._engines[run.session_id] = engine
        # The first turn is the task itself. The framing matters: instructions often restate the
        # schedule ("every day at 5:32pm…"), so make explicit that the schedule already fired and
        # the job now is to execute, not to (re)schedule.
        opening = (
            f"⏰ Scheduled run — {task.title}\n\n"
            "This automation is due now: carry out the task below immediately and produce the "
            "result. The schedule already exists — do not create or modify any scheduled tasks.\n\n"
            f"{task.instructions}"
        )
        try:
            async for _event in engine.run(opening):
                pass
            run.result_text = _last_assistant_text(engine.messages)
            run.artifacts = _recent_files(task.workspace, since=run.started_at)
            run.status = "ok"
            if task.notify_on_completion:
                await self._notify_task_done(task, run)
        except Exception as exc:
            run.status, run.error = "error", str(exc)
        finally:
            run.finished_at = _epoch()
            # post_run hooks (E2): fire after run.status is determined. Best-effort —
            # results are ignored (a hook can't change the run outcome post-hoc), but a
            # failing hook is recorded, not propagated. Mirrors NotifyRouter.dispatch_run.
            try:
                self.hooks.fire(
                    "post_run",
                    {
                        "event": "post_run",
                        "task_id": task.id,
                        "task_name": task.title,
                        "session_id": run.session_id,
                        "workspace": task.workspace,
                        "agent": task.agent,
                        "run_status": run.status,
                        "error": run.error,
                        "result_text": (run.result_text or "")[:1000],
                    },
                )
            except Exception:
                pass
            # Persist the run as a continuable session + keep the live engine for an immediate
            # follow-up; record the run (now carrying its session_id).
            try:
                self.save(run.session_id, engine)
                self._engines[run.session_id] = engine
            except Exception:
                pass
            self.task_store.add_run(run)
        return run

    async def _notify_task_done(self, task, run: TaskRun) -> None:
        summary = (run.result_text or "").strip()[:280]
        # Notify any socket viewing this scheduled run's session (it's a durable session of its own).
        await self.broadcast_session(
            run.session_id,
            {
                "type": "task_done",
                "data": {
                    "task": task.title,
                    "id": task.id,
                    "text": summary,
                    "run_id": run.run_id,
                },
            },
        )
        # UX-026: app-wide "automation finished" toast. Pairs with automation_run_started —
        # a user who stepped away (or is in another section) learns the run completed, with a
        # View-run link. broadcast_session only reaches sockets already on this session; this
        # reaches every open window via /ws/events.
        await self.broadcast_event(
            {
                "type": "automation_run_done",
                "data": {
                    "task_id": task.id,
                    "task_title": task.title,
                    "session_id": run.session_id,
                    "workspace": task.workspace,
                    "agent": task.agent,
                    "run_id": run.run_id,
                    "status": run.status,
                    "summary": summary,
                },
            }
        )
        # Messaging-target + multi-channel notify fan-out (sync core, shared with manual runs).
        self._dispatch_run_notify(task, run)

    def _dispatch_run_notify(self, task, run: TaskRun) -> None:
        """Sync core of run-completion notification: the single messaging target (if set) +
        the NotifyRouter multi-channel fan-out (钉钉/飞书/企微/webhook/邮件). Shared by the
        headless scheduled path (``_notify_task_done``) and the synchronous manual-run
        finalizer (``finalize_manual_run``), which can't await. Best-effort: a failing channel
        is logged inside the router, never raised. Whether anything actually sends is gated by
        ``task.notify_level`` + ``run.status`` inside the router (none/important may no-op)."""
        summary = (run.result_text or "").strip()[:280]
        if task.notify_target:
            from ..connectors.base import parse_target
            from ..connectors.senders import DEFAULT_SENDERS

            try:
                platform, chat_id, thread = parse_target(task.notify_target)
                sender = DEFAULT_SENDERS.get(platform)
                creds = self.secrets.get(f"{platform}:default") or {}
                if sender and creds.get("bot_token"):
                    sender(creds["bot_token"], chat_id, f"✓ {task.title}\n\n{summary}", thread)
            except Exception:
                pass
        try:
            self.notify_router.dispatch_run(
                task_name=task.title,
                run_status=run.status,
                result_text=run.result_text,
                error=run.error,
                channels=task.notify_channels or None,
                level=task.notify_level,
            )
        except Exception:
            pass

    async def _notify_inbox_action_needed(self, item) -> None:
        """Fan a pending decision (approval/plan/directory) from an automation run out through
        the NotifyRouter. This is the "your automation is blocked and needs you" alarm — it runs
        INDEPENDENTLY of the messaging-channel mirror, so a user with only notify channels
        (钉钉/飞书/企微) and no bound Slack/Telegram still gets pinged when they're away.

        Scope rules:
          - Only protected decision kinds (approval/plan/directory). Questions (ask_user) are
            lower-stakes and usually have a live socket; skip to avoid noise.
          - Only items carrying task context (``data.task_title``) — i.e. a scheduled/unattended
            run. Interactive sessions have the user in-app; no alarm needed.
          - Respect ``task.notify_level``: ``none`` stays silent; ``important``/``all`` sends.
            A pending decision IS the important event, so we bypass the router's status filter
            (which would suppress non-error) and decide here instead.
        """
        if item.state != "pending":
            return  # durable resume re-raised an already-resolved prompt — no alarm
        if item.kind not in {"approval", "plan", "directory"}:
            return
        data = getattr(item, "data", None) or {}
        task_title = data.get("task_title")
        task_id = data.get("task_id")
        if not task_title:
            return  # no automation context → user is in an interactive session, in-app already
        task = self.task_store.get(task_id) if task_id else None
        level = (task.notify_level if task else "important")
        if level == "none":
            return
        # Compose a body that names the tool/decision and points back to the app.
        body = "\n".join(p for p in (item.title, item.body) if p).strip()[:1500]
        body = f"{body}\n\n打开 OpenWorker 处理。"
        try:
            await asyncio.to_thread(
                lambda: self.notify_router.dispatch(
                    title=f"[{task_title}] 需要你处理",
                    body=body,
                    status="ok",  # not a run-status; level gate already decided above
                    channels=(task.notify_channels if task and task.notify_channels else None),
                    level=level,
                )
            )
        except Exception:
            pass

    # -- automation REST --------------------------------------------------------
    def list_automations(self) -> dict[str, Any]:
        # Unseen = runs started after the task's seen mark (UX-023 sidebar badges).
        # `unseen_failed` tints the badge when the NEWEST unseen run errored.
        tasks = []
        for t in self.task_store.list():
            unseen = [
                r for r in self.task_store.runs(t.id) if r.started_at > t.seen_runs_at
            ]
            tasks.append(
                {
                    **t.public(),
                    "unseen_runs": len(unseen),
                    "unseen_failed": bool(unseen) and unseen[0].status == "error",
                }
            )
        return {"tasks": tasks}

    def mark_automation_seen(self, task_id: str) -> dict[str, Any]:
        task = self.task_store.get(task_id)
        if task is None:
            return {"ok": False, "error": "not found"}
        task.seen_runs_at = time.time()
        self.task_store.save(task)
        return {"ok": True}

    def get_automation(self, task_id: str) -> dict[str, Any]:
        task = self.task_store.get(task_id)
        if task is None:
            return {"error": "not found"}
        return {
            "task": task.public(),
            "runs": [r.to_dict() for r in self.task_store.runs(task_id)],
        }

    def create_automation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create an automation directly from the GUI (the "New automation" / template flow).
        Mirrors the agent-facing `create_scheduled_task` validation, but binds the task to a
        fresh per-task scratch workspace instead of an origin conversation's folder."""
        from croniter import croniter

        title = (payload.get("title") or "").strip()
        instructions = (payload.get("instructions") or "").strip()
        cron = (payload.get("cron") or "").strip() or None
        fire_at = (payload.get("fire_at") or "").strip() or None
        timezone = (payload.get("timezone") or "").strip() or "local"

        if not title:
            return {"ok": False, "error": "title is required"}
        if not instructions:
            return {"ok": False, "error": "instructions are required"}
        if not cron and not fire_at:
            return {
                "ok": False,
                "error": "provide a cron (recurring) or a fire_at ISO datetime (one-time)",
            }
        if cron and not croniter.is_valid(cron):
            return {"ok": False, "error": f"invalid cron expression: {cron}"}

        schedule = Schedule(
            kind="once" if (fire_at and not cron) else "cron",
            cron=cron,
            fire_at=fire_at,
            timezone=timezone,
        )
        from ..automation.models import grant_entries

        task = ScheduledTask(
            title=title,
            instructions=instructions,
            schedule=schedule,
            workspace="",
            origin_surface="cowork",
            agent="cowork",
            # Human-driven path (GUI form / onboarding recipes): the creating surface
            # rendered the grants, the submit IS the consent. Same validation as the
            # agent tool — only target-bound write grants survive.
            always_allowed_tools=grant_entries(payload.get("permissions")),
            notify_channels=list(payload.get("notify_channels") or []),
            notify_level=str(payload.get("notify_level") or "important"),
        )
        task.workspace = self._provision_scratch(task.task_session_id)
        self.task_store.save(task)
        return {"ok": True, "task": task.public()}

    def update_automation(
        self, task_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        task = self.task_store.get(task_id)
        if task is None:
            return {"ok": False, "error": "not found"}
        if "enabled" in changes:
            task.enabled = bool(changes["enabled"])
        if changes.get("instructions") is not None:
            task.instructions = changes["instructions"]
        if changes.get("title") is not None:
            task.title = changes["title"]
        if changes.get("cron") is not None:
            from croniter import croniter

            if not croniter.is_valid(changes["cron"]):
                return {"ok": False, "error": "invalid cron"}
            task.schedule.cron, task.schedule.kind = changes["cron"], "cron"
        if changes.get("notify_channels") is not None:
            task.notify_channels = list(changes["notify_channels"] or [])
        if changes.get("notify_level") is not None:
            task.notify_level = str(changes["notify_level"])
        if changes.get("revoke"):
            # Revocation from the task detail page ("Allowed without asking … · Revoke").
            # Human-only, like minting; the agent-facing update tool has no such field.
            task.revoke_rule(str(changes["revoke"]))
        self.task_store.save(task)
        if changes.get("revoke"):
            # A live run engine may still hold the revoked rule — reseed from the record.
            for sid, engine in self._engines.items():
                owner = self.task_store.task_for_run_session(sid)
                if owner is not None and owner.id == task.id:
                    engine.permissions.task_rules = task.standing_rules()
        return {"ok": True, "task": task.public()}

    def delete_automation(self, task_id: str) -> dict[str, Any]:
        return {"ok": self.task_store.delete(task_id), "id": task_id}

    # -- digital humans (DHP bridge, 批次 B) ------------------------------------
    def list_digital_humans(self, category: Optional[str] = None) -> dict[str, Any]:
        """Catalog listing from the DHP registry (index.json entries, no full specs).

        Surfaces per-source load errors in ``source_errors`` so the UI can show *why* the catalog
        is empty (e.g. the official source's HTTP fetch failed) instead of a silent blank store.
        """
        entries = self.dhp_registry.list(category=category)
        installed = {i.slug for i in self.dhp_instances.list()}
        return {
            "ok": True,
            "humans": [dict(e.to_dict(), installed=e.slug in installed) for e in entries],
            "categories": self.dhp_registry.categories(),
            "source_errors": self.dhp_registry.source_errors(),
        }

    def get_digital_human(self, slug: str) -> dict[str, Any]:
        """Full spec for one digital human (catalog entry + parsed spec + config_schema form)."""
        entry = self.dhp_registry.get(slug)
        if entry is None:
            return {"ok": False, "error": f"digital human {slug!r} not found"}
        try:
            spec = self.dhp_registry.get_spec(slug)
        except Exception as e:  # SpecError or file error
            return {"ok": False, "error": str(e)}
        # If an installed instance has a config_schema override (form-editor edits), apply it so the
        # editor sees the edited schema, not the registry's original.
        inst = self.dhp_instances.get_by_slug(slug)
        if inst is not None and inst.config_schema_override:
            from ..digital_human.spec import apply_schema_override
            try:
                apply_schema_override(spec, inst.config_schema_override, slug)
            except Exception as e:
                return {"ok": False, "error": f"config_schema override is invalid: {e}"}
        return {
            "ok": True,
            "entry": entry.to_dict(),
            "spec": spec.to_dict(),
            "requires_consent": {
                "mcps": [m.to_dict() for m in spec.requires_mcps],
                "skills": [s.to_dict() for s in spec.requires_skills],
                "plugins": [p.to_dict() for p in spec.requires_plugins],
                "commands": [c.to_dict() for c in spec.requires_commands],
                "subagents": [s.to_dict() for s in spec.requires_subagents],
                "rules": [r.to_dict() for r in spec.requires_rules],
                "hooks": [h.to_dict() for h in spec.requires_hooks],
                "permissions": list(spec.permissions),
                "browser_login": list(spec.browser_login),
                "has_schedule": spec.primary_schedule is not None,
            },
        }

    def preflight_digital_human(self, slug: str, config: dict[str, Any]) -> dict[str, Any]:
        """Compute the dependency/capability manifest + an approval digest BEFORE installing.

        The GUI shows this manifest (source, version, requires plugins/mcps/skills/...,
        capabilities, permissions) and asks the user to confirm. The install endpoint must
        receive the matching approval digest back; a digest mismatch (spec changed between
        preflight and install) re-runs preflight and refuses the stale approval. Plugins that
        register an MCP server are called out for SEPARATE confirmation — they are not
        auto-installed under the single install approval.
        """
        from ..digital_human import validate_config
        import hashlib

        try:
            spec = self.dhp_registry.get_spec(slug)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        cfg = dict(config or {})
        missing = validate_config(spec, cfg)
        if missing:
            return {"ok": False, "error": f"missing required config: {', '.join(missing)}"}

        entry = self.dhp_registry.get(slug)
        source = ""
        version = spec.version
        if entry is not None:
            # The registry entry records which source it came from (provenance).
            source = getattr(entry, "source_id", "") or ""

        # Dependency/capability manifest: every external thing this spec pulls in.
        # Rules + hooks are soft deps: preflight reports whether each is already
        # registered (configured: True/False) so the GUI can flag needs_attention.
        # A missing rule/hook does NOT block install — the user may add it manually.
        registered_rules = {
            (r["pattern"], r["action"]) for r in self.rule_store.list(enabled_only=True)
        }
        registered_hook_keys = {
            (h["event"], h.get("match_tool", "*")) for h in self.hooks.list(enabled_only=True)
        }
        manifest = {
            "source": source,
            "version": version,
            "spec_version": spec.spec_version,
            "requires_plugins": [p.to_dict() for p in spec.requires_plugins],
            "requires_mcps": [m.to_dict() for m in spec.requires_mcps],
            "requires_skills": [s.to_dict() for s in spec.requires_skills],
            "requires_commands": [c.to_dict() for c in spec.requires_commands],
            "requires_subagents": [s.to_dict() for s in spec.requires_subagents],
            "requires_rules": [
                {
                    **r.to_dict(),
                    "configured": (r.pattern, r.action) in registered_rules,
                }
                for r in spec.requires_rules
            ],
            "requires_hooks": [
                {
                    **h.to_dict(),
                    "configured": (h.event, h.match_tool) in registered_hook_keys,
                }
                for h in spec.requires_hooks
            ],
            "permissions": list(spec.permissions),
            "browser_login": list(spec.browser_login),
            "config_secret_keys": [f.key for f in spec.config_schema if f.is_secret],
        }
        # Plugins that register an MCP server need a SEPARATE explicit confirmation — they
        # expand the agent's tool surface at runtime, which is a higher-risk action than a
        # plain plugin install. Surface them so the GUI can collect a second ack.
        mcp_registering_plugins = []
        for dep in spec.requires_plugins:
            # We can't know without the plugin manifest whether it registers an MCP server;
            # flag any plugin whose id suggests MCP (heuristic) or all of them for separate
            # confirmation if the spec also declares requires_mcps.
            if spec.requires_mcps:
                mcp_registering_plugins.append(dep.id)

        # Stable approval digest: binds (slug, version, manifest) so a spec that changed
        # between preflight and install is caught. SHA-256 over the canonical JSON.
        digest_payload = json.dumps(
            {"slug": slug, "version": version, "manifest": manifest},
            sort_keys=True, ensure_ascii=False,
        )
        digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
        # Soft-dep gaps: rules/hooks the spec wants but aren't registered yet. These don't
        # block install (the user may add them manually or accept the default path), but the
        # GUI surfaces them so the user knows the DH's intended guardrails won't be active.
        needs_attention: list[str] = []
        for r in manifest["requires_rules"]:
            if not r["configured"]:
                needs_attention.append(
                    f"rule {r['pattern']!r} → {r['action']} (not registered)"
                )
        for h in manifest["requires_hooks"]:
            if not h["configured"]:
                needs_attention.append(
                    f"hook {h['event']} on {h.get('match_tool', '*')} (not registered)"
                )
        return {
            "ok": True,
            "manifest": manifest,
            "approval_digest": digest,
            "mcp_confirmation_required": mcp_registering_plugins,
            "needs_attention": needs_attention,
            "missing_required_config": missing,
        }

    def install_digital_human(
        self,
        slug: str,
        config: dict[str, Any],
        *,
        approval_digest: str = "",
        mcp_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Install a digital human as an automation (spec → ScheduledTask + instance record).

        Requires an ``approval_digest`` matching the current preflight manifest; a stale or
        absent digest is refused (dependency plan changed since the user approved). Plugins
        that register an MCP server require a separate ``mcp_confirmed`` ack — they are not
        auto-installed under the single install approval. Missing non-MCP plugins from the
        default marketplace source ARE auto-installed (one-click dependency resolution), but
        only after the digest check passes.
        """
        from ..digital_human import install_digital_human, SpecError

        try:
            spec = self.dhp_registry.get_spec(slug)
        except SpecError as e:
            return {"ok": False, "error": str(e)}

        # Approval boundary: recompute the preflight digest and require a match. Without this,
        # a spec that gained a new plugin/MCP dependency between the user viewing the manifest
        # and clicking install would silently pull in the new capability.
        preflight = self.preflight_digital_human(slug, dict(config or {}))
        if not preflight.get("ok"):
            return preflight
        current_digest = preflight["approval_digest"]
        if not approval_digest or approval_digest != current_digest:
            return {
                "ok": False,
                "error": "dependency plan changed; review and re-approve the manifest",
                "approval_digest": current_digest,
                "manifest": preflight["manifest"],
            }
        if preflight.get("mcp_confirmation_required") and not mcp_confirmed:
            return {
                "ok": False,
                "error": "this digital human registers MCP-backed plugins; confirm the MCP registration separately",
                "mcp_confirmation_required": preflight["mcp_confirmation_required"],
            }

        # Auto-install missing non-MCP plugin dependencies from the default marketplace source.
        # MCP-registering plugins are NOT auto-installed here — they need their own confirmation.
        if spec.requires_plugins:
            installed_plugins = {p["name"] for p in self.list_plugins() if p.get("name")}
            mcp_plugin_ids = set(preflight.get("mcp_confirmation_required") or [])
            missing = [
                d for d in spec.requires_plugins
                if d.id not in installed_plugins and d.id not in mcp_plugin_ids
            ]
            for dep in missing:
                # Find the plugin in the default source's catalog, then install it.
                default_src = next((s for s in self.plugin_sources.list() if s.is_default), None)
                if default_src is None:
                    break
                catalog = self.list_plugin_catalog(default_src.id)
                if not catalog.get("ok"):
                    break
                cat_names = {c["name"] for c in catalog.get("plugins", [])}
                if dep.id in cat_names:
                    self.install_plugin(default_src.id, dep.id)

        result = install_digital_human(
            spec,
            dict(config or {}),
            task_store=self.task_store,
            scratch_provider=self._provision_scratch,
            instances=self.dhp_instances,
        )
        return result

    def list_dh_instances(self) -> dict[str, Any]:
        insts = self.dhp_instances.list()
        out = []
        for inst in insts:
            task = self.task_store.get(inst.task_id)
            out.append(
                dict(
                    inst.to_dict(),
                    task=task.public() if task else None,
                )
            )
        return {"ok": True, "instances": out}

    def uninstall_digital_human(self, instance_id: str) -> dict[str, Any]:
        from ..digital_human import uninstall_digital_human

        return uninstall_digital_human(
            instance_id, instances=self.dhp_instances, task_store=self.task_store
        )

    # -- DHP source management (批次 D2) ----------------------------------------
    def list_dhp_sources(self) -> dict[str, Any]:
        return {"ok": True, "sources": [s.to_dict() for s in self.dhp_sources.list()]}

    def add_dhp_source(self, name: str, url: str, *, source_type: str = "dhp") -> dict[str, Any]:
        from ..digital_human import DhpRegistry

        try:
            src = self.dhp_sources.add(name, url, source_type=source_type)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        # Rebuild the registry so the new source is live without a restart.
        self.dhp_registry = DhpRegistry(self.dhp_sources.list(enabled_only=True))
        return {"ok": True, "source": src.to_dict()}

    def update_dhp_source(self, source_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        from ..digital_human import DhpRegistry

        src = self.dhp_sources.update(source_id, changes or {})
        if src is None:
            return {"ok": False, "error": "source not found"}
        self.dhp_registry = DhpRegistry(self.dhp_sources.list(enabled_only=True))
        return {"ok": True, "source": src.to_dict()}

    def remove_dhp_source(self, source_id: str) -> dict[str, Any]:
        from ..digital_human import DhpRegistry

        ok = self.dhp_sources.remove(source_id)
        if not ok:
            return {"ok": False, "error": "source not found"}
        self.dhp_registry = DhpRegistry(self.dhp_sources.list(enabled_only=True))
        return {"ok": True}

    def reset_dhp_sources(self) -> dict[str, Any]:
        """Restore all builtin DHP sources (undoes deletions). The 'reset to defaults' escape hatch."""
        from ..digital_human import DhpRegistry

        sources = self.dhp_sources.reset()
        self.dhp_registry = DhpRegistry(self.dhp_sources.list(enabled_only=True))
        return {"ok": True, "sources": [s.to_dict() for s in sources]}

    # -- DHP instance edit (批次 D2) -------------------------------------------
    def update_digital_human(self, instance_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Edit an installed digital human's config / prompt / schedule / notify.

        Rebuilds the task instructions from the spec + new config (preserving the ``## 用户配置``
        preamble), then patches the linked ScheduledTask via :meth:`update_automation` (no separate
        task-patch logic). Secrets are routed to SecretStore; non-secret config to the instance.
        """
        from ..digital_human import reinstall_instructions, validate_config, split_config
        from ..digital_human.instances import SECRET_PROFILE_PREFIX
        from ..digital_human.spec import apply_schema_override

        inst = self.dhp_instances.get(instance_id)
        if inst is None:
            return {"ok": False, "error": "instance not found"}
        try:
            spec = self.dhp_registry.get_spec(inst.slug)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        changes = changes or {}

        # If the instance already has a config_schema override (from prior form-editor edits), apply
        # it so validation/split/instructions use the edited schema, not the registry's original.
        if inst.config_schema_override:
            try:
                apply_schema_override(spec, inst.config_schema_override, inst.slug)
            except Exception as e:
                return {"ok": False, "error": f"stored config_schema override is invalid: {e}"}

        # A new config_schema in changes replaces the override. Validate it by applying it to the
        # spec (raises SpecError on bad shape) before persisting, so an invalid edit is rejected
        # without touching the instance.
        new_schema = changes.get("config_schema")
        if new_schema is not None:
            try:
                apply_schema_override(spec, new_schema, inst.slug)
            except Exception as e:
                return {"ok": False, "error": f"config_schema is invalid: {e}"}
            inst.config_schema_override = list(new_schema) if isinstance(new_schema, list) else []

        task_changes: dict[str, Any] = {}
        user_config = changes.get("user_config")
        system_prompt = changes.get("system_prompt")

        # Only rebuild instructions when config or prompt actually changed.
        # A config_schema edit also forces a rebuild — the preamble must reflect the new schema, and
        # existing config values must be re-validated/default-filled against it.
        if user_config is not None or system_prompt is not None or new_schema is not None:
            merged = self.dhp_instances.resolve_config(inst)  # current non-secret + secret values
            if user_config is not None:
                merged.update(user_config)
            # Re-validate + default-fill against the schema (mutates merged in place).
            validate_config(spec, merged)
            non_secret, secret, secret_keys = split_config(spec, merged)
            if secret_keys and self.dhp_instances.secrets is not None:
                self.dhp_instances.secrets.put(f"{SECRET_PROFILE_PREFIX}{inst.id}", secret)
            inst.config = non_secret
            inst.secret_keys = secret_keys
            # Instructions carry only non-secret config + typed ``<configured>`` markers for
            # secret keys — the values never enter the static task prompt/API/audit. Same
            # boundary as the install path.
            task_changes["instructions"] = reinstall_instructions(
                spec, non_secret, system_prompt=system_prompt, secret_keys=secret_keys
            )
            # The rewrite produces a clean (no-plaintext-secret) preamble; clear the legacy flag.
            inst.needs_secret_migration = False

        for key in ("cron", "notify_channels", "notify_level", "title", "enabled"):
            if key in changes:
                task_changes[key] = changes[key]

        if task_changes:
            result = self.update_automation(inst.task_id, task_changes)
            if not result.get("ok"):
                return result

        self.dhp_instances.put(inst)
        task = self.task_store.get(inst.task_id)
        return {"ok": True, "instance": dict(inst.to_dict(), task=task.public() if task else None)}

    # -- DHP dependency health (批次 D2) ---------------------------------------
    def dhp_mcp_health(self, slug: str) -> dict[str, Any]:
        """For each MCP the spec requires, report whether it's configured + connected."""
        try:
            spec = self.dhp_registry.get_spec(slug)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # list_mcp() returns a list[dict] directly (the /v1/mcp HTTP wrapper
        # is what wraps it in {"servers": [...]}).
        by_name = {m.get("name"): m for m in self.list_mcp()}
        out = []
        for dep in spec.requires_mcps:
            m = by_name.get(dep.id)
            out.append(
                {
                    "name": dep.id,
                    "reason": dep.reason,
                    "configured": m is not None,
                    "status": (m or {}).get("status", "missing"),
                    "tool_count": (m or {}).get("tool_count", 0),
                }
            )
        return {"ok": True, "items": out}

    def dhp_skills_health(self, slug: str) -> dict[str, Any]:
        """For each skill the spec requires, report whether it's installed."""
        try:
            spec = self.dhp_registry.get_spec(slug)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # list_skills() returns a list[dict] with a "name" key (the /v1/skills
        # HTTP wrapper is what wraps it in {"skills": [...]}).
        installed = {s["name"] for s in self.list_skills() if s.get("name")}
        out = []
        for dep in spec.requires_skills:
            out.append(
                {
                    "id": dep.id,
                    "reason": dep.reason,
                    "installed": dep.id in installed,
                }
            )
        return {"ok": True, "items": out}

    def dhp_plugins_health(self, slug: str) -> dict[str, Any]:
        """For each plugin the spec requires, report whether it's installed."""
        try:
            spec = self.dhp_registry.get_spec(slug)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        installed = {p["name"] for p in self.list_plugins() if p.get("name")}
        out = []
        for dep in spec.requires_plugins:
            out.append({
                "id": dep.id,
                "reason": dep.reason,
                "installed": dep.id in installed,
            })
        return {"ok": True, "items": out}

    def dhp_commands_health(self, slug: str) -> dict[str, Any]:
        """For each command the spec requires, report whether it's available."""
        try:
            spec = self.dhp_registry.get_spec(slug)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        installed = {c["name"] for c in self.list_commands() if c.get("name")}
        out = []
        for dep in spec.requires_commands:
            out.append({
                "id": dep.id,
                "reason": dep.reason,
                "installed": dep.id in installed,
            })
        return {"ok": True, "items": out}

    def dhp_subagents_health(self, slug: str) -> dict[str, Any]:
        """For each subagent persona the spec requires, report whether it's available."""
        try:
            spec = self.dhp_registry.get_spec(slug)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        available = {p.get("id") for p in self.personas.list_all() if p.get("id")}
        out = []
        for dep in spec.requires_subagents:
            out.append({
                "id": dep.id,
                "reason": dep.reason,
                "installed": dep.id in available,
            })
        return {"ok": True, "items": out}

    def dhp_upgrade_check(self, instance_id: str) -> dict[str, Any]:
        """Compare an instance's installed spec_version against the registry's current version."""
        inst = self.dhp_instances.get(instance_id)
        if inst is None:
            return {"ok": False, "error": "instance not found"}
        entry = self.dhp_registry.get(inst.slug)
        current = entry.version if entry else ""
        return {
            "ok": True,
            "installed_version": inst.spec_version,
            "latest_version": current,
            "up_to_date": bool(current) and inst.spec_version == current,
        }

    def prepare_manual_run(self, task_id: str) -> dict[str, Any]:
        """Create a 'running' manual run and return its session, so the GUI can open it and
        drive the task LIVE over the normal session WS (you watch the agent + follow up). The
        automatic scheduler path stays headless (`_run_scheduled_task`)."""
        task = self.task_store.get(task_id)
        if task is None:
            return {"ok": False, "error": "not found"}
        Path(task.workspace).mkdir(parents=True, exist_ok=True)
        run = TaskRun(
            task_id=task.id, trigger="manual"
        )  # status "running", session_id auto
        self.task_store.add_run(run)
        return {
            "ok": True,
            "run_id": run.run_id,
            "session_id": run.session_id,
            "workspace": task.workspace,
            "agent": task.agent,
            # Same execute-now framing as the headless path — manual runs ride a normal live
            # session whose engine DOES have scheduling tools, so be explicit.
            "prompt": (
                f"⏰ Running automation '{task.title}' now. Carry out these instructions "
                "immediately and produce the result. The schedule already exists — do not create "
                f"or modify any scheduled tasks.\n\n{task.instructions}"
            ),
        }

    def finalize_manual_run(self, task_id: str, run_id: str) -> dict[str, Any]:
        """Mark a manual run complete once its first turn finished (the WS already saved the
        session). Pulls result text + artifacts from the persisted transcript/workspace.
        """
        run = next(
            (r for r in self.task_store.runs(task_id) if r.run_id == run_id), None
        )
        task = self.task_store.get(task_id)
        if run is None or task is None:
            return {"ok": False, "error": "not found"}
        if run.status == "running":
            record = self.session_store.load(run.session_id)
            run.result_text = _last_assistant_text(record.messages) if record else None
            run.artifacts = _recent_files(task.workspace, since=run.started_at)
            run.status = "ok"
            run.finished_at = _epoch()
            self.task_store.add_run(run)
            task.last_run, task.last_status = run.finished_at, "ok"
            task.run_count += 1
            self.task_store.save(task)
            # Manual-run completion notification. Unlike the headless scheduled path, the user
            # drove this run over a live WS so we skip the socket broadcast — but a user who
            # stepped away mid-run still gets told it finished via their notify channels (and
            # messaging target, if set). Level-gated same as scheduled runs: ``important``
            # (default) stays silent on success, ``all`` pings every time.
            if task.notify_on_completion:
                self._dispatch_run_notify(task, run)
        return {"ok": True, "run": run.to_dict()}

    def save(self, session_id: str, engine: TurnEngine) -> None:
        executor = getattr(engine, "executor", None)
        workspace = os.path.realpath(str(executor.cwd)) if executor else ""
        self.session_store.save(
            SessionRecord(
                session_id=session_id,
                workspace=workspace,
                model=engine.model,
                mode=engine.permissions.mode.value,
                messages=engine.messages,
                title=title_from(engine.messages),
                agent=getattr(engine, "agent_name", "code"),
                extra_roots=self._extra_roots_of(engine),
                grants=_grants_of(engine),
                compaction=(
                    engine.compaction_state.as_dict()
                    if getattr(engine, "compaction_state", None)
                    else {}
                ),
            )
        )

    @staticmethod
    def _apply_grants(engine: TurnEngine, grants: dict[str, Any]) -> None:
        """Re-apply a reloaded session's persisted "Always allow" approvals — they're
        session-scoped, and the session outlives the process (owner-hit 2026-07-22)."""
        for tool in grants.get("tools") or []:
            engine.permissions.allow_tool_for_session(str(tool))
        for command in grants.get("commands") or []:
            engine.permissions.allow_command_for_session(str(command))

    @staticmethod
    def _extra_roots_of(engine: TurnEngine) -> list[dict[str, Any]]:
        """Added folders = the engine's roots minus the primary scratch (index 0)."""
        roots = getattr(engine, "roots", None) or []
        return [
            {"path": str(r.path), "writable": bool(r.writable), "label": r.label}
            for r in roots[1:]
        ]

    # -- LLM auto-titles (FB-010) -------------------------------------------------
    _AUTOTITLE_PROMPT = (
        "你负责为聊天会话起标题。根据用户的开场消息，仅以 4-5 个词回复一个会话标题——"
        "不要加引号或标点包裹。如果开场只是寒暄或闲聊、没有实质话题"
        "（「嘿」「你好吗」「在吗」），则精确回复：闲聊"
    )

    def _maybe_autotitle(self, session_id: str) -> None:
        """Kick off title generation after a turn completes, fire-and-forget. Only while
        the session has neither a manual rename nor a generated title, at most twice:
        attempt 1 rides turn 1, and the second window exists solely for the small-talk
        retry (with both openers). Attempts are counted in memory rather than derived
        from the user-message count — steering injections also land as role "user", and
        counting them would silently suppress titling on a steered first turn. A restart
        forgetting the counter is harmless: renamed/auto_title still gate re-titling."""
        if session_id.startswith("__"):
            return
        engine = self._engines.get(session_id)
        if engine is None or session_id in self._autotitle_inflight:
            return
        if self.task_store.task_for_run_session(session_id) is not None:
            return  # automation runs are titled by their task
        if self._autotitle_attempts.get(session_id, 0) >= 2:
            return
        users = [m for m in engine.messages if m.get("role") == "user"]
        if not users:
            return
        state = self.session_store.title_state(session_id)
        if state is None or state["renamed"] or state["auto_title"]:
            return
        from ..attachments import content_to_text

        openers = [
            text
            for m in users
            if (text := content_to_text(m.get("content"), image_placeholder="").strip())
        ][:2]
        if not openers:
            return
        self._autotitle_attempts[session_id] = (
            self._autotitle_attempts.get(session_id, 0) + 1
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop to ride (sync caller) — skip, never block
        self._autotitle_inflight.add(session_id)
        # Retain the task: the loop holds only a weak ref, and a GC'd task would both
        # kill the title mid-flight and strand the inflight guard.
        task = loop.create_task(self._generate_autotitle(session_id, engine, openers))
        self._autotitle_tasks.add(task)
        task.add_done_callback(self._autotitle_tasks.discard)

    async def _generate_autotitle(
        self, session_id: str, engine: TurnEngine, openers: list[str]
    ) -> None:
        """One cheap non-streaming completion on the session's own provider/model. Every
        failure (provider error, empty, absurdly long) is swallowed — the title_from
        fallback stays; the small-talk sentinel leaves auto_title unset so the turn-2
        retry can run."""
        try:
            turn = await asyncio.to_thread(
                engine.provider.complete,
                model=engine.model,
                messages=[
                    {"role": "system", "content": self._AUTOTITLE_PROMPT},
                    {"role": "user", "content": "\n\n".join(openers)},
                ],
                temperature=0.2,
                # Reasoning-routed models spend hidden tokens BEFORE emitting text; a
                # tight cap plus default effort yields an empty completion and a silent
                # no-op. Effort "none" reaches only the OpenAI-compat path (the native
                # providers whitelist their settings), and 64 leaves headroom either way.
                max_tokens=64,
                reasoning_effort="none",
            )
            raw = (getattr(turn, "text", None) or "").strip()
            # Sanitize: surrounding quotes off, whitespace collapsed, capped at 60.
            title = " ".join(raw.strip("\"'“”‘’`").split())
            # Sentinel tolerance: models riff on the exact token ("Small talk.", quoted,
            # trailing period) — normalize before comparing, else the riff becomes the title.
            if title.lower().strip(".!,;:'\"").replace(" ", "-").replace("_", "-") in (
                "small-talk",
                "smalltalk",
                "闲聊",
            ):
                return
            if not title or len(title) > 80:
                return
            if self.session_store.set_auto_title(session_id, title[:60]):
                # Best-effort nudge for any live viewer; the sidebar's poll and
                # post-turn refresh pick the new title up regardless.
                await self.broadcast_session(
                    session_id,
                    {
                        "type": "session_title",
                        "data": {"session_id": session_id, "title": title[:60]},
                    },
                )
        except Exception:
            # A failed title must never surface as a session error — but it must
            # not be invisible either (a silent provider 400 hid the max_tokens
            # rejection for a whole owner test pass, 2026-07-20).
            logger.debug("autotitle failed for %s", session_id, exc_info=True)
        finally:
            self._autotitle_inflight.discard(session_id)

    # -- session roots (orphan Cowork: scratch + added folders) ------------------
    def get_roots(self, session_id: str) -> list[dict[str, Any]]:
        """The directories this session can touch: primary scratch first, then added folders.
        Reads the live engine when one is running; otherwise reconstructs from persisted state.
        """
        engine = self._engines.get(session_id)
        if engine is not None and getattr(engine, "roots", None):
            return [
                {
                    "path": str(r.path),
                    "writable": bool(r.writable),
                    "label": r.label,
                    "primary": i == 0,
                    "exists": r.path.is_dir(),
                }
                for i, r in enumerate(engine.roots)
            ]
        record = self.session_store.load(session_id)
        primary = (
            record.workspace
            if record and record.workspace
            else self._provision_scratch(session_id)
        )
        extra = (record.extra_roots if record else []) or []
        out = [
            {
                "path": primary,
                "writable": True,
                "label": "scratch",
                "primary": True,
                "exists": Path(primary).is_dir(),
            }
        ]
        for r in extra:
            p = str(r.get("path", ""))
            out.append(
                {
                    "path": p,
                    "writable": bool(r.get("writable", False)),
                    "label": r.get("label") or Path(p).name,
                    "primary": False,
                    "exists": Path(p).is_dir(),
                }
            )
        return out

    def add_root(
        self, session_id: str, path: str, writable: bool = False
    ) -> dict[str, Any]:
        """Grant the session access to another folder (read-only or read-write). Mutates the live
        engine in place when running (file tools + permissions + context see it immediately) and
        persists it so a later resume still has it."""
        p = Path(path).expanduser()
        if not p.is_dir():
            return {"ok": False, "error": f"not a directory: {path}"}
        resolved = p.resolve()
        engine = self._engines.get(session_id)
        if engine is not None and getattr(engine, "roots", None) is not None:
            if any(r.path == resolved for r in engine.roots):
                # already present: just update its access level
                for r in engine.roots:
                    if r.path == resolved:
                        r.writable = bool(writable)
            else:
                engine.roots.append(RootDir(path=resolved, writable=bool(writable)))
            self.session_store.set_extra_roots(session_id, self._extra_roots_of(engine))
        else:
            # A brand-new conversation has no record yet (it's only saved after the first turn) —
            # create one now so set_extra_roots has a row to update and the folder survives.
            if self.session_store.load(session_id) is None:
                self.session_store.save(
                    SessionRecord(
                        session_id=session_id,
                        workspace=self._provision_scratch(session_id),
                        model=self.model,
                        mode=self.mode.value,
                        messages=[],
                        agent="cowork",  # folder access is a Cowork affordance
                    )
                )
            extra = [r for r in self.get_roots(session_id) if not r["primary"]]
            extra = [r for r in extra if Path(r["path"]).resolve() != resolved]
            extra.append(
                {
                    "path": str(resolved),
                    "writable": bool(writable),
                    "label": resolved.name,
                }
            )
            self.session_store.set_extra_roots(
                session_id,
                [
                    {
                        "path": r["path"],
                        "writable": r["writable"],
                        "label": r.get("label", ""),
                    }
                    for r in extra
                ],
            )
        self.session_store.touch_workspace(str(resolved))
        return {"ok": True, "roots": self.get_roots(session_id)}

    def remove_root(self, session_id: str, path: str) -> dict[str, Any]:
        """Revoke a previously-added folder. The primary scratch cannot be removed."""
        resolved = Path(path).expanduser().resolve()
        engine = self._engines.get(session_id)
        if engine is not None and getattr(engine, "roots", None):
            if engine.roots and engine.roots[0].path == resolved:
                return {
                    "ok": False,
                    "error": "cannot remove the primary scratch directory",
                }
            engine.roots[:] = [r for r in engine.roots if r.path != resolved]
            self.session_store.set_extra_roots(session_id, self._extra_roots_of(engine))
        else:
            current = self.get_roots(session_id)
            if (
                current
                and current[0]["primary"]
                and Path(current[0]["path"]).resolve() == resolved
            ):
                return {
                    "ok": False,
                    "error": "cannot remove the primary scratch directory",
                }
            extra = [
                r
                for r in current
                if not r["primary"] and Path(r["path"]).resolve() != resolved
            ]
            self.session_store.set_extra_roots(
                session_id,
                [
                    {
                        "path": r["path"],
                        "writable": r["writable"],
                        "label": r.get("label", ""),
                    }
                    for r in extra
                ],
            )
        return {"ok": True, "roots": self.get_roots(session_id)}

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        # A live engine's in-memory thread is authoritative: mid-turn it's ahead of the
        # persisted record — which may not even exist yet for a scheduled run's first turn
        # (opening a "running" automation showed a blank session; owner report 2026-07-04).
        engine = self._engines.get(session_id)
        if engine is not None:
            return list(engine.messages)
        record = self.session_store.load(session_id)
        return record.messages if record else []

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        if session_id.startswith("__"):
            return {"ok": False, "error": "internal sessions cannot be renamed"}
        ok = self.session_store.rename(session_id, title)
        return {
            "ok": ok,
            "session_id": session_id,
            "title": " ".join((title or "").split())[:120],
        }

    def set_session_flags(
        self,
        session_id: str,
        *,
        pinned: Optional[bool] = None,
        archived: Optional[bool] = None,
    ) -> dict[str, Any]:
        if session_id.startswith("__"):
            return {"ok": False, "error": "internal sessions cannot be modified here"}
        ok = self.session_store.set_flags(session_id, pinned=pinned, archived=archived)
        return {"ok": ok, "session_id": session_id}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        if session_id.startswith("__"):
            return {"ok": False, "error": "internal sessions cannot be deleted here"}
        engine = self._engines.pop(session_id, None)
        if engine is not None:
            try:
                # (was engine.interrupt() — a method that never existed; the AttributeError
                # was silently swallowed, so deleting a running session never stopped it.)
                engine.request_interrupt()
            except Exception:
                pass
        record = self.session_store.load(session_id)
        ok = self.session_store.delete(session_id)
        # Deleting a session is the one implicit unsubscribe (otherwise subscriptions are permanent).
        self.subscriptions.remove_session(session_id)
        # ...and releases any Slack threads it owned (§31): the next tag there spawns fresh.
        self.mention_sessions.remove_session(session_id)
        # ...and drops its per-session connector overrides (§4.2, like subscriptions).
        self.session_connections.remove_session(session_id)
        # ...and its per-session skill mutes (SKILLS-SPEC §3 — mutes die with the session).
        self.session_skills.remove_session(session_id)
        # ...and closes its pending Inbox items — an orphaned approval/question can never be
        # meaningfully answered (owner call, 2026-07-03).
        self.inbox.resolve_session(session_id)
        # ...and its scratch dir. STRICTLY scoped: only a directory inside scratch_base is
        # removed — a real project folder the user picked is never touched.
        if ok and record and record.workspace:
            scratch = self.scratch_base().resolve()
            ws = Path(record.workspace)
            try:
                resolved = ws.resolve()
                if (
                    resolved.is_relative_to(scratch)
                    and resolved != scratch
                    and resolved.is_dir()
                ):
                    shutil.rmtree(resolved)
            except OSError:
                pass  # a stale/foreign path must not fail the delete
        return {"ok": ok, "session_id": session_id}

    # -- provider proxy ---------------------------------------------------------
    def provider_complete(self, model, messages, tools=None):
        return self.provider.complete(model=model, messages=messages, tools=tools)

    def _refresh_provider(self, name: Optional[str] = None) -> None:
        """Drop the router's cached client(s) so the next turn rebuilds with fresh config.
        No-op for an injected non-router provider (tests)."""
        invalidate = getattr(self.provider, "invalidate", None)
        if callable(invalidate):
            invalidate(name)

    # -- read models ------------------------------------------------------------
    def list_sessions(self, workspace: Optional[str] = None) -> list[dict[str, Any]]:
        ws = self.resolve_workspace(workspace) if workspace else None
        return [
            {
                "session_id": r.session_id,
                "title": r.title or "New session",
                "workspace": r.workspace,
                "agent": r.agent,
                "model": r.model,
                "mode": r.mode,
                "updated_at": r.updated_at,
                "messages": r.message_count,
                "pinned": r.pinned,
                "archived": r.archived,
                # §31: non-user origin ("slack") + display label — drives the sidebar's
                # "From Slack" group and the row's platform icon.
                "origin": r.origin,
                "origin_label": r.origin_label,
                # Attention = Inbox items awaiting this session (the amber count that bubbles
                # session → persona → footer Inbox). Liveness = working (in-flight turn) /
                # sleeping (a self-wake is pending) / idle — a count-less dot that never bubbles.
                "attention": len(self.inbox.pending(session_id=r.session_id)),
                "liveness": self._session_liveness(r.session_id),
                # Channels this session listens to (inbound subscriptions) — drives the per-session
                # "connections" indicator.
                "subscriptions": [
                    s.channel for s in self.subscriptions.for_session(r.session_id)
                ],
            }
            for r in self.session_store.list(workspace=ws)
            if not r.session_id.startswith("__")  # hide internal threads
        ]

    def _session_liveness(self, session_id: str) -> str:
        if self.is_running(session_id):
            return "working"
        if self.wakes.pending(session_id):
            return "sleeping"
        return "idle"

    def list_agents(self) -> list[dict[str, Any]]:
        return _list_agents()

    # -- skills (SKILLS-SPEC §4.4) ------------------------------------------------
    def list_skills(self, workspace: Optional[str] = None) -> list[dict[str, Any]]:
        """Enriched rows for the Settings screen (scope/source/enabled). Optional workspace
        adds that project's skills, with project copies shadowing same-named global ones.
        Plugin-contributed skills (E4 packaging) are appended as a synthetic 'plugin' scope."""
        rows = self.skill_store.rows(workspace or None)
        # E4 plugin skills live outside the store's global/project dirs; surface them so the
        # Settings screen shows every discoverable skill, not just the folder-CRUD ones.
        plugin_rows = self._plugin_skill_rows()
        if plugin_rows:
            existing = {r["name"] for r in rows}
            for pr in plugin_rows:
                if pr["name"] not in existing:
                    rows.append(pr)
        return rows

    def reveal_skill(
        self, name: str, workspace: Optional[str] = None
    ) -> dict[str, Any]:
        """Open the skill's folder in the OS file manager (§6 "Show folder" — the power-user
        window into folder-is-truth). Same local-machine rationale as reveal_artifact."""
        import subprocess
        import sys

        try:
            folder, _scope = self.skill_store.find(name, workspace or None)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["open", str(folder)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif sys.platform == "win32":
                import os

                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(
                    ["xdg-open", str(folder)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def effective_skill_names(
        self, session_id: str, workspace: Optional[str | Path] = None
    ) -> set[str]:
        """The session's skill menu (§3): merged scopes − Settings disables − session mutes.
        The single resolver behind the engine catalog, the rail list, and the composer popup."""
        dirs = [self.skill_store.global_dir]
        if workspace:
            dirs.append(self.skill_store.project_dir(workspace))
        loader = SkillLoader(dirs)
        return effective_skills(
            names=set(loader.names()),
            disabled=self.skill_store.disabled_names(),
            session_overrides=self.session_skills.get(session_id),
        )

    def session_skills_view(
        self, session_id: str, workspace: Optional[str] = None
    ) -> dict[str, Any]:
        """The rail payload: every in-scope, Settings-enabled skill with its mute state."""
        disabled = self.skill_store.disabled_names()
        overrides = self.session_skills.get(session_id)
        rows = [
            {
                "name": r["name"],
                "description": r["description"],
                "scope": r["scope"],
                "enabled": overrides.get(r["name"], True),
            }
            for r in self.skill_store.rows(workspace or None)
            if r["name"] not in disabled
        ]
        return {"skills": rows}

    def _scratch_workspace_error(self, workspace: Any) -> Optional[dict[str, Any]]:
        """Refuse skill WRITES into a per-conversation scratch dir — a skill saved there is
        stranded in a throwaway folder. Backend chokepoint: guards every entry path (UI,
        REST, future import), not just the flows the GUI happens to gate."""
        if not workspace:
            return None
        try:
            ws = Path(str(workspace)).expanduser().resolve()
            if ws.is_relative_to(self.scratch_base().resolve()):
                return {
                    "ok": False,
                    "error": (
                        "该文件夹是临时会话空间——保存在那里的技能会丢失。"
                        "请全局保存或选择一个真实的项目。"
                    ),
                }
        except OSError:
            pass
        return None

    def create_skill(self, body: dict[str, Any]) -> dict[str, Any]:
        blocked = self._scratch_workspace_error(body.get("workspace"))
        if blocked:
            return blocked
        try:
            created = self.skill_store.create(
                name=str(body.get("name", "")),
                description=str(body.get("description", "")),
                instructions=str(body.get("instructions", "")),
                scope=str(body.get("scope", "global") or "global"),
                workspace=body.get("workspace") or None,
                allowed_tools=_parse_allowed_tools(body.get("allowed_tools")),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "skill": created}

    def update_skill(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            if "enabled" in body:
                self.skill_store.set_enabled(name, bool(body["enabled"]))
            if (
                body.get("description") is not None
                or body.get("instructions") is not None
                or body.get("allowed_tools") is not None
            ):
                self.skill_store.update(
                    name,
                    description=body.get("description"),
                    instructions=body.get("instructions"),
                    workspace=body.get("workspace") or None,
                    allowed_tools=_parse_allowed_tools(body.get("allowed_tools")),
                )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def delete_skill(self, name: str, workspace: Optional[str] = None) -> dict[str, Any]:
        try:
            self.skill_store.delete(name, workspace or None)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def move_skill(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        # Moving INTO project scope must not target a scratch dir (moving OUT is fine —
        # that's the rescue path for already-stranded skills).
        if str(body.get("scope", "")) == "project":
            blocked = self._scratch_workspace_error(body.get("workspace"))
            if blocked:
                return blocked
        try:
            moved = self.skill_store.move(
                name,
                to_scope=str(body.get("scope", "")),
                workspace=body.get("workspace") or None,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "skill": moved}

    def stage_skill_upload(self, data: bytes, filename: str = "") -> dict[str, Any]:
        try:
            preview = self.skill_store.stage_upload(data, filename)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **preview}

    def confirm_skill_upload(self, body: dict[str, Any]) -> dict[str, Any]:
        blocked = self._scratch_workspace_error(body.get("workspace"))
        if blocked:
            return blocked
        try:
            saved = self.skill_store.confirm_upload(
                str(body.get("token", "")),
                scope=str(body.get("scope", "global") or "global"),
                workspace=body.get("workspace") or None,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "skill": saved}

    def _plugin_skill_dirs(self) -> list[Path]:
        """Skill subfolders contributed by installed plugins (state_dir()/plugins/<n>/skills)."""
        dirs: list[Path] = []
        plugins_root = state_dir() / "plugins"
        if not plugins_root.is_dir():
            return dirs
        for sub in sorted(plugins_root.iterdir()):
            sd = sub / "skills"
            if sd.is_dir():
                dirs.append(sd)
        return dirs

    def _plugin_skill_rows(self) -> list[dict[str, Any]]:
        """Plugin-contributed skills as enriched rows (synthetic 'plugin' scope). Read-only —
        plugins are managed via the E4 install layer, not the folder-CRUD store."""
        rows: list[dict[str, Any]] = []
        for sd in self._plugin_skill_dirs():
            loader = SkillLoader([sd])
            for entry in loader.catalog():
                rows.append(
                    {
                        "name": entry["name"],
                        "description": entry.get("description", ""),
                        "scope": "plugin",
                        "source": "plugin",
                        "enabled": True,  # read-only; toggling is a no-op
                        "writable": False,
                    }
                )
        return rows

    def _plugin_command_dirs(self) -> list[Path]:
        """Command subfolders contributed by installed plugins (state_dir()/plugins/<n>/commands)."""
        dirs: list[Path] = []
        plugins_root = state_dir() / "plugins"
        if not plugins_root.is_dir():
            return dirs
        for sub in sorted(plugins_root.iterdir()):
            cd = sub / "commands"
            if cd.is_dir():
                dirs.append(cd)
        return dirs

    def _tool_hook_firer(self):
        """Return a firer for tool-level hooks (pre_tool/post_tool/on_message), or None.

        Returns None when no enabled tool-event hook is registered, so the engine's hot
        path skips the subprocess call entirely. The firer is the HookStore.fire bound
        method — it runs external commands (30s timeout, best-effort) and never raises.
        """
        from ..hooks import TOOL_EVENTS

        has_tool_hook = any(
            h["enabled"] and h["event"] in TOOL_EVENTS
            for h in self.hooks.list(enabled_only=True)
        )
        return self.hooks.fire if has_tool_hook else None

    def _engine_command_loader(self):
        """Build a CommandLoader that scans both standalone commands and plugin-contributed ones.

        Used by build_engine so the agent's instructions include commands that shipped inside
        an installed plugin (state_dir()/plugins/<n>/commands/). Built fresh each call so a
        newly-installed plugin's commands are visible without a restart.
        """
        from ..commands import CommandLoader

        return CommandLoader([state_dir() / "commands", *self._plugin_command_dirs()])

    # -- skill sources + install/uninstall (批次 E1) -----------------------------
    # Mirrors the DHP source-management surface: list/add/update/remove sources, browse a
    # source's catalog, install a named skill (copies SKILL.md folder into state_dir()/skills),
    # and uninstall by name. Errors surface as {"ok": False, "error": ...} for the UI.

    def list_skill_sources(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.skill_sources.list()]

    def add_skill_source(self, name: str, url: str, *, source_type: str = "http") -> dict[str, Any]:
        try:
            src = self.skill_sources.add(name, url, source_type=source_type)
            return {"ok": True, "source": src.to_dict()}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def update_skill_source(self, source_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        src = self.skill_sources.update(source_id, changes)
        if src is None:
            return {"ok": False, "error": "source not found"}
        return {"ok": True, "source": src.to_dict()}

    def remove_skill_source(self, source_id: str) -> dict[str, Any]:
        if not self.skill_sources.remove(source_id):
            return {"ok": False, "error": "source not found"}
        return {"ok": True, "id": source_id}

    def list_skill_catalog(self, source_id: str) -> dict[str, Any]:
        """Browse installable skills from one source (fetches/clones on demand)."""
        from ..skills import list_catalog as _list_catalog

        src = self.skill_sources.get(source_id)
        if src is None:
            return {"ok": False, "error": "source not found"}
        try:
            items = _list_catalog(src, self._skill_cache_root)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        installed = {s["name"] for s in self.list_skills() if s.get("name")}
        return {
            "ok": True,
            "source": src.to_dict(),
            "skills": [{**it, "installed": it["name"] in installed} for it in items],
        }

    def install_skill(self, source_id: str, name: str) -> dict[str, Any]:
        from ..skills import install_skill as _install, SkillInstallError

        src = self.skill_sources.get(source_id)
        if src is None:
            return {"ok": False, "error": "source not found"}
        try:
            return _install(src, name, skills_dir=state_dir() / "skills", cache_root=self._skill_cache_root)
        except SkillInstallError as e:
            return {"ok": False, "error": str(e)}

    def uninstall_skill(self, name: str) -> dict[str, Any]:
        from ..skills import uninstall_skill as _uninstall, SkillInstallError

        try:
            return _uninstall(name, skills_dir=state_dir() / "skills")
        except SkillInstallError as e:
            return {"ok": False, "error": str(e)}

    # -- Plugins (marketplace install/uninstall, 批次 E4) -----------------------
    # Claude-Code-format plugins installed from marketplace sources. The official
    # claude-plugins-official marketplace is built in. Plugins land under
    # state_dir()/plugins/<name>/; their skills/commands subfolders are picked up by the
    # existing loaders (see _plugin_skill_dirs / _plugin_command_dirs). MCP servers
    # declared in .mcp.json or plugin.json's mcpServers are registered on install and
    # unregistered on uninstall (tracked in the plugin registry).
    def list_plugins(self) -> list[dict[str, Any]]:
        from ..plugins import list_installed as _list_installed

        return _list_installed(state_dir() / "plugins", self.plugin_registry)

    def list_plugin_sources(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.plugin_sources.list()]

    def add_plugin_source(self, name: str, url: str, *, source_type: str = "git") -> dict[str, Any]:
        try:
            src = self.plugin_sources.add(name, url, source_type=source_type)
            return {"ok": True, "source": src.to_dict()}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def update_plugin_source(self, source_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        src = self.plugin_sources.update(source_id, changes)
        if src is None:
            return {"ok": False, "error": "source not found"}
        return {"ok": True, "source": src.to_dict()}

    def remove_plugin_source(self, source_id: str) -> dict[str, Any]:
        if not self.plugin_sources.remove(source_id):
            return {"ok": False, "error": "source not found"}
        return {"ok": True, "id": source_id}

    def list_plugin_catalog(self, source_id: str) -> dict[str, Any]:
        """Browse installable plugins from one marketplace (clones on demand)."""
        from ..plugins import list_catalog as _list_catalog

        src = self.plugin_sources.get(source_id)
        if src is None:
            return {"ok": False, "error": "source not found"}
        try:
            items = _list_catalog(src, self._plugin_cache_root)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        installed = {p.name for p in self.plugin_registry.list()}
        categories = sorted({i["category"] for i in items if i.get("category")})
        return {
            "ok": True,
            "source": src.to_dict(),
            "plugins": [{**it, "installed": it["name"] in installed} for it in items],
            "categories": categories,
        }

    def install_plugin(self, source_id: str, name: str) -> dict[str, Any]:
        from ..plugins import install_plugin as _install, PluginInstallError, InstalledPlugin

        src = self.plugin_sources.get(source_id)
        if src is None:
            return {"ok": False, "error": "source not found"}
        try:
            result = _install(
                src, name,
                plugins_dir=state_dir() / "plugins",
                cache_root=self._plugin_cache_root,
                mcp_register=self._register_plugin_mcp,
            )
        except PluginInstallError as e:
            return {"ok": False, "error": str(e)}
        # Record in the registry so uninstall can reverse the MCP registrations + the
        # loader-scan picks up the plugin's skills/commands.
        import datetime as _dt

        self.plugin_registry.add(InstalledPlugin(
            name=result["name"],
            version=result.get("version") or "",
            description=result.get("description") or "",
            source_id=source_id,
            source_info=result.get("source_info") or {},
            components=result.get("components") or {},
            installed_at=_dt.datetime.now().isoformat(timespec="seconds"),
            sha=result.get("sha") or "",
        ))
        return result

    def _register_plugin_mcp(self, server_name: str, config: dict[str, Any]) -> None:
        """Register an MCP server contributed by a plugin (called during install)."""
        self.add_mcp(server_name, config)

    def uninstall_plugin(self, name: str) -> dict[str, Any]:
        from ..plugins import uninstall_plugin as _uninstall, PluginInstallError

        try:
            return _uninstall(
                name,
                plugins_dir=state_dir() / "plugins",
                registry=self.plugin_registry,
                mcp_unregister=self.delete_mcp,
            )
        except PluginInstallError as e:
            return {"ok": False, "error": str(e)}

    def check_plugin_updates(self) -> dict[str, Any]:
        """Check all installed plugins against their marketplace's latest sha."""
        from ..plugins import check_updates as _check_updates

        out: list[dict[str, Any]] = []
        for src in self.plugin_sources.list(enabled_only=True):
            out.extend(_check_updates(src, self.plugin_registry, self._plugin_cache_root))
        return {"ok": True, "items": out}

    def update_plugin(self, name: str) -> dict[str, Any]:
        """Re-install a plugin from its source to pull the latest version."""
        entry = self.plugin_registry.get(name)
        if entry is None:
            return {"ok": False, "error": "plugin not installed"}
        src = self.plugin_sources.get(entry.source_id)
        if src is None:
            return {"ok": False, "error": "source no longer configured"}
        # Uninstall (clears registry + MCP), then re-install (pulls latest sha).
        self.uninstall_plugin(name)
        return self.install_plugin(entry.source_id, name)

    # -- Persona marketplace sources (批次 E4 后续) ---------------------------
    # Git repos of *.md persona manifests the user can browse + install from. Mirrors
    # plugin_sources / skill_sources. Installing reuses PersonaRegistry.install_from_dir
    # (consent summary + snapshot + disabled-pending-approval), so the marketplace never
    # changes the trust model — no executable code, lands disabled pending consent.
    def list_persona_sources(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.persona_sources.list()]

    def add_persona_source(self, name: str, url: str, *, source_type: str = "git") -> dict[str, Any]:
        try:
            src = self.persona_sources.add(name, url, source_type=source_type)
            return {"ok": True, "source": src.to_dict()}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def update_persona_source(self, source_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        src = self.persona_sources.update(source_id, changes)
        if src is None:
            return {"ok": False, "error": "source not found"}
        return {"ok": True, "source": src.to_dict()}

    def remove_persona_source(self, source_id: str) -> dict[str, Any]:
        if not self.persona_sources.remove(source_id):
            return {"ok": False, "error": "source not found or is built-in (disable it instead)"}
        return {"ok": True, "id": source_id}

    def list_persona_catalog(self, source_id: str) -> dict[str, Any]:
        """Browse installable personas from one marketplace (clones on demand)."""
        from ..personas import list_catalog as _list_catalog, PersonaMarketplaceError

        src = self.persona_sources.get(source_id)
        if src is None:
            return {"ok": False, "error": "source not found"}
        try:
            items = _list_catalog(src, self._persona_cache_root)
        except PersonaMarketplaceError as e:
            return {"ok": False, "error": str(e)}
        installed = {p["id"] for p in self.personas.list_all() if p.get("id")}
        return {
            "ok": True,
            "source": src.to_dict(),
            "personas": [{**it, "installed": it["id"] in installed} for it in items],
        }

    def install_persona_from_source(self, source_id: str, persona_id: str) -> dict[str, Any]:
        """Install one persona from a marketplace source. Returns the consent summary
        the registry produces; the persona lands disabled pending the user's approval."""
        from ..personas import install_persona as _install, PersonaMarketplaceError

        src = self.persona_sources.get(source_id)
        if src is None:
            return {"ok": False, "error": "source not found"}
        try:
            result = _install(src, persona_id, registry=self.personas, cache_root=self._persona_cache_root)
        except PersonaMarketplaceError as e:
            return {"ok": False, "error": str(e)}
        # Refresh the catalog's `installed` flags + return the full persona list so the
        # caller can refresh its UI in one round-trip (mirrors install_plugin).
        return {**result, "personas": self.personas.list_all()}

    # -- Rules (allow/deny/ask permission layer, 批次 E2) --------------------
    def list_rules(self) -> list[dict[str, Any]]:
        return self.rule_store.list()

    def add_rule(self, pattern: str, action: str, *, reason: str = "") -> dict[str, Any]:
        try:
            r = self.rule_store.add(pattern, action, reason=reason)
            return {"ok": True, "rule": r}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def update_rule(self, rule_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.rule_store.update(rule_id, changes)
            if r is None:
                return {"ok": False, "error": "rule not found"}
            return {"ok": True, "rule": r}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def remove_rule(self, rule_id: str) -> dict[str, Any]:
        return {"ok": self.rule_store.remove(rule_id)}

    # -- Hooks (pre_run/post_run, 批次 E2) -----------------------------------
    def list_hooks(self) -> list[dict[str, Any]]:
        return self.hooks.list()

    def add_hook(
        self,
        name: str,
        event: str,
        command: str,
        *,
        match: str = "*",
        match_tool: str = "*",
    ) -> dict[str, Any]:
        try:
            h = self.hooks.add(name, event, command, match=match, match_tool=match_tool)
            return {"ok": True, "hook": h}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def update_hook(self, hook_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        try:
            h = self.hooks.update(hook_id, changes)
            if h is None:
                return {"ok": False, "error": "hook not found"}
            return {"ok": True, "hook": h}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def remove_hook(self, hook_id: str) -> dict[str, Any]:
        return {"ok": self.hooks.remove(hook_id)}

    # -- Commands (slash templates, 批次 E3) ---------------------------------
    # Read-only: command files live under state_dir()/commands/<name>/COMMAND.md and are
    # hand-authored or installed via E4 plugin packaging. The frontend lists them for the
    # Composer "/" autocomplete and fetches the full template on selection.
    def list_commands(self) -> list[dict[str, Any]]:
        from ..commands import CommandLoader

        loader = CommandLoader([state_dir() / "commands", *self._plugin_command_dirs()])
        return loader.catalog()

    def get_command(self, name: str) -> Optional[dict[str, Any]]:
        from ..commands import CommandLoader

        loader = CommandLoader([state_dir() / "commands", *self._plugin_command_dirs()])
        c = loader.get(name)
        if c is None:
            return None
        return {
            "name": c.name,
            "description": c.description,
            "prompt_template": c.prompt_template,
            "allowed_tools": c.allowed_tools,
        }

    def list_memory(self) -> list[dict[str, Any]]:
        return [
            {"id": m.id, "scope": m.scope.value, "content": m.content}
            for m in self.memory_store.list()
        ]

    def add_memory(
        self, content: str, scope: str = "workspace", workspace: Optional[str] = None
    ) -> dict[str, Any]:
        chosen = Scope(scope) if scope in _SCOPES else Scope.WORKSPACE
        ws = self.resolve_workspace(workspace) if chosen is Scope.WORKSPACE else None
        item = self.memory_store.add(content, scope=chosen, workspace=ws)
        return {"id": item.id, "scope": item.scope.value, "content": item.content}


def _parse_inbox_json(s: str) -> dict[str, Any]:
    """Parse a structured Inbox resolution (directory/plan carry their reply as a JSON string)."""
    import json as _json

    try:
        v = _json.loads(s) if s else {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _epoch() -> float:
    import time

    return time.time()


# A Slack message ts looks like "1700000001.000001" (epoch seconds + microseconds). Other
# platforms use opaque/incrementing ids (e.g. a Telegram integer), so only parse the Slack shape.
_SLACK_TS_RE = re.compile(r"^\d+\.\d+$")


def _inbound_epoch(message_id: Optional[str]) -> float:
    """Best-effort epoch-seconds for a MessageSource: a Slack-style ts, else wall-clock now."""
    if message_id and _SLACK_TS_RE.match(str(message_id)):
        try:
            return float(message_id)
        except ValueError:
            pass
    return time.time()


def _last_assistant_text(messages: list[dict[str, Any]]) -> Optional[str]:
    for msg in reversed(messages or []):
        if msg.get("role") == "assistant" and msg.get("content"):
            return msg["content"]
    return None


def _recent_files(workspace: str, *, since: float, limit: int = 20) -> list[str]:
    """Files in the task workspace modified during the run — the run's artifacts."""
    out: list[str] = []
    root = Path(workspace)
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            if path.is_file() and path.stat().st_mtime >= since - 1:
                out.append(str(path.relative_to(root)))
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


def _artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".xlsx", ".xls"}:
        return "sheet"
    if suffix in {".pptx", ".ppt", ".pptm", ".docx", ".doc", ".docm"}:
        return "office"
    if suffix in {".csv", ".tsv"}:
        return "csv"
    if suffix in {".py", ".js", ".ts", ".tsx", ".css", ".json"}:
        return "code"
    return "text"


def _redact(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy of a server config safe to return over REST — env/header values masked."""
    out = dict(raw)
    for key in ("env", "headers"):
        if isinstance(out.get(key), dict):
            out[key] = {k: ("***" if v else v) for k, v in out[key].items()}
    return out


def _git_branch(path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=3,
        )
        branch = result.stdout.strip()
        return branch or None
    except (OSError, subprocess.SubprocessError):
        return None
