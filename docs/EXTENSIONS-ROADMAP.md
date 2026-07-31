# OpenWorker-Pro 扩展体系路线图（批次 E 及之后）

> 本路线图回答：如何让 openworker 支持 Plugins / MCPs / Skills / Subagents / Rules / Commands / Hooks，并让数字人与这些紧密联动。可自定义插件源（如接入 `https://github.com/anthropics/claude-plugins-official.git`）。

## 核心判断：不造轮子，造胶水

调研结论：openworker **已有 70% 骨架**，不是从零开始。缺的是"统一市场 + 安装链路 + 数字人联动"。

| 概念 | openworker 现状 | 缺口 |
|---|---|---|
| **MCPs** | ✅ 全链路：`list/add/remove_mcp` + connect/signout/reload + tools + `/v1/mcp/*` 路由 + 前端 McpTab（Integrations▸Connectors/MCP）| 无市场源、无批量安装、数字人 requires_mcps 只做健康检查不做一键补装 |
| **Skills** | ✅ Anthropic SKILL.md 格式（progressive disclosure）+ `SkillLoader` 扫 `state_dir()/skills` + `list_skills` + `/v1/skills` | ❌ 无 install/uninstall API、❌ 无前端管理 UI、无市场源 |
| **Subagents** | ⚠️ Persona 即 subagent 载体：`PersonaManifest` 有 `tools/skills/mcp/recommends` 字段，`PersonaManifest.to_agent()` 组装 runtime Agent | ❌ 无"子代理委托"运行时（agent 调 agent，主代理把子任务委派给专门 persona）、❌ 无并行化（多个子代理同时跑）、❌ 无上下文隔离（子代理在自己 context window 跑，不污染主对话）、persona 无市场源、无数字人↔persona 绑定 |
| **Rules** | ⚠️ `overrides.py`（`RiskOverrideStore`）是雏形：glob 匹配工具名调 risk class，user-local 不可被 persona 写 | 只做 risk 一维，无 allow/deny/ask 三态规则、无"持久系统级指令引导代理"层、无前端 UI、数字人不能声明规则 |
| **Commands** | ❌ 无 slash command 机制 | 全缺 |
| **Hooks** | ❌ 无 PreToolUse/PostToolUse 钩子 | 全缺（scheduler 有 run 前后通知，但不是 tool 级钩子）|
| **Plugins** | ⚠️ DHP digital human spec.yaml 算一种插件形态（声明 system_prompt/requires/config_schema）| 无统一插件抽象、无插件源市场、不能跨 MCP+skill+persona+command 打包 |
| **连接器** | ✅ 重体系：`BasePlatformAdapter` + `Gateway` + 10+ 连接器（Slack/GitHub/Gmail/HubSpot/企微…）| 与 MCP 是两套并行，认知负担大；数字人 requires 不覆盖 connector |
| **内置浏览器** | ✅ Playwright 工具集（`browser_automation.py`），有 approval/capabilities 元数据 | ❌ browser_login 仅 spec 声明层，无登录态持久化/复用 |
| **自定义源** | ✅ DHP 有 `SourceManager`（多源 HTTP index，已做）| 只服务于数字人，不覆盖 MCP/skill/plugin |

## 架构原则

### 核心规则（两条，统领整个扩展体系）

这两条规则是后续所有批次（E1-E5）的设计宪法——每个扩展点的实现都要回过头来对这两条做对照：

**规则 1 — 统一扩展市场（Extension Hub）**

MCP / Skill / Persona / Plugin / Command / Hook / Rule **全部走同一套链路**：

```
源（source） → 索引（index/catalog） → 安装（install） → 启用（enable） → 数字人声明依赖（requires） → 健康检查（health）
```

- 复用 DHP 已验证的 `SourceManager + Adapter` 模式（`coworker/digital_human/sources.py` + `adapters.py`）：源持久化在 prefs、内置源不可删只可禁、HTTP/git/local 三种 adapter。
- 每个扩展类有自己的 `SourceManager`（Skill 已做 E1、Persona/MCP/Command/Hook/Rule 待 E2-E4），但它们共享同一个 `Adapter` 抽象和同一套安装/卸载动作语义。
- Customize 页面（已做）就是这个 Hub 的统一着陆页——7 个分类横排按钮，点哪个看哪个的已装项 + 市场浏览安装。

**规则 2 — 数字人是 customize 的消费者，不是平行体系**

数字人（DHP digital human）是扩展的**消费者**，它声明自己需要哪些扩展，安装数字人时自动补装缺失依赖（一键就绪）：

- spec.yaml 的 `requires_mcps` / `requires_skills`（当前分散字段）升级为统一 `requires`：`{mcps, skills, connectors, browser_login, rules, subagents, commands}`。
- 安装数字人时，对 `requires` 做全量检查——缺失的扩展自动弹"是否一并安装"（E1 已为 skills 做了安装链路，后续批次补齐其他类的安装后，数字人一键补装自然覆盖全类型）。
- DhEditPanel 的健康面板当前只覆盖 MCP/skills 健康（`dhp_mcp_health` / `dhp_skills_health`），扩展到全类型——每项 `requires` 都有对应的"已装/缺失/版本不匹配"状态，缺失项旁可直接点「安装」。
- 这条规则确保数字人和 customize 是**同一个体系**：在 customize 装的扩展，数字人能立刻声明依赖；数字人缺的依赖，能从 customize 一键补。不存在"数字人用一套、customize 管另一套"的割裂。

### 其他原则

3. **不破坏现有两套连接体系**：connectors（OAuth 账号 + IM relay）和 MCP（工具服务器）保持各司其职，但都纳入 Extension Hub 的"已装扩展"视图。
4. **合规红线不变**：不碰 ilink 个人微信逆向协议。插件源可接入官方仓库，但每个源标注信任等级。

## 分批规划

### 批次 E1：Skills 全链路（最高性价比，skills 现在是半成品）

**为什么先做**：skills 后端加载已完整（`SkillLoader` 扫 `state_dir()/skills/<name>/SKILL.md`，YAML frontmatter `name/description/allowed-tools` + markdown 正文 + progressive disclosure：会话开始只注入 catalog，正文按需 `load_skill` 加载）。只差安装链路和 UI。工程量最小，立刻补齐"数字人 requires_skills 一键补装"。

**后端** `coworker/skills/store.py`（新建，仿 DHP `sources.py` 的 `SourceManager`）：
- `SkillSourceManager`：源持久化（`skill_sources` prefs 键），内置源 `anthropic-official` → `https://github.com/anthropics/skills`（官方 skill 仓库）+ `openworker-community`。
- `install_skill(source_id, name)`：从源 clone/fetch → 落到 `state_dir()/skills/<name>/`（含 `SKILL.md` + 资源）。复用 DHP `adapters.py` 的 `DhpHttpAdapter`（HTTP index 源）+ `LocalRepoAdapter`（本地/git 源）抽象。
- `uninstall_skill(name)`：删目录（内置 skill 不可删，标记 `builtin`）。
- `list_skill_sources()` / `add_skill_source()` / `remove_skill_source()`。
- `manager.py` 接入：`install_skill/uninstall_skill/list_skill_sources/add_skill_source`。

**app.py 路由**：
- `POST /v1/skills/install` `{source_id, name}` → 安装
- `DELETE /v1/skills/{name}` → 卸载
- `GET/POST/PATCH/DELETE /v1/skills/sources` → 源 CRUD

**前端** `CustomizeView.tsx` 的 Skills 区块升级（当前只读列表）：
- 已装技能列表：name + description + **卸载**按钮（调 `DELETE /v1/skills/{name}`）。
- 源管理（复用 DhpSourcesSection 模式，泛化）+ 市场浏览（按源列出可装 skill + **安装**按钮）。
- `api.ts` 加 `installSkill/uninstallSkill/getSkillSources/addSkillSource/removeSkillSource`。
- DhEditPanel 的 `DhSkillsBlock` 升级：缺失技能旁加「安装」按钮（调 `install_skill`），装完刷新健康检查。

**数字人联动**：install digital human 时，`requires_skills` 缺失项自动弹"是否一并安装"。

---

### 批次 E2：Rules 三态 + Hooks 雏形 ✅

**Rules —— 持久系统级指令引导代理**

规则为代理提供**持久的、系统级的指令**，以满足编码标准和工作流。两层含义：
1. **工具权限规则**（allow / deny / ask）：**已做（含 E2.5 引擎接入）**。新建 `coworker/rules.py`（`RuleStore`），**不重写** `overrides.py`——`overrides.py`（`RiskOverrideStore`）是更底层的 risk-class 放宽器（read/write_local/exec/external），驱动权限引擎的决策树；`rules.py` 是用户侧的显式权限层（allow/deny/ask），两者并存。规则结构 `{pattern: glob, action: allow|deny|ask, reason, enabled, id}`，glob 匹配工具名（如 `mcp__notion__*`），最具体规则获胜（复用 `overrides._specificity` 的 specificity 算法）。`allow`→风险降为 read（绕过审批），`deny`→阻断，`ask`→强制审批。`/v1/rules` CRUD + 前端 RulesPanel（pattern 输入 + action 下拉 + reason + 删除，内联新增）。**E2.5 已接引擎**：`PermissionEngine` 加 `rule_resolver` 字段，`evaluate()` 在 risk=classify() 之前先查规则——deny 硬阻断、allow 绕过审批、ask 强制审批（即使 AUTO 模式下 deny 仍阻断，用户的显式权限声明是最高优先级层）。`build_engine` 加 `rule_resolver` 参数，manager 两个调用点传 `self.rule_store.resolver()`。**待做**：数字人 spec 加 `requires_rules`。
2. **行为引导规则**（系统指令层）：**待做**。`{when: always|tool_match, instruction: str}`，注入到 agent 的 system prompt。例如"所有 PR 描述用 conventional commits 格式"、"修改 React 组件后跑 `npm test`"。这是用户工作流标准化的一部分，比 prompt 模板更持久（不依赖每次手输）。

**Hooks —— 代理循环期间运行的钩子**

在代理循环（agent loop）期间运行的钩子将在此处显示。事件点：
- `pre_tool` / `post_tool`：工具调用前后（可拦改参数、记录、阻断）。**待做**
- `pre_run` / `post_run`：数字人调度运行前后。**已做**——`coworker/hooks/store.py`（`HookStore`），接入 `manager.py:_run_scheduled_task`：pre_run 在 engine build 前触发（hook 可输出 `{"skip": true}` 跳过运行），post_run 在 finally 块 run.status 确定后触发。context 以 JSON 传 stdin，subprocess 执行，30s 超时，失败不阻断运行。
- `on_message`：消息进出时。**待做**

Hook = name + event + match（glob 限定哪些任务名触发）+ command（shell 或脚本路径）+ enabled。`/v1/hooks` CRUD + 前端 HooksPanel（name + event 下拉 + match + command + enabled toggle + 删除）。先做 `pre_run/post_run`（数字人调度前后，最实用），后做 tool 级。

**数字人联动**：**待做**。数字人可声明 `hooks`（如 `post_run` 发 Slack 通知——这正好接已有的 NotifyRouter + connectors）。spec `requires` 加 `rules`/`hooks` 子键。

**E2 完成度**：Rules（CRUD + UI + 三态 resolve）+ Hooks（CRUD + UI + pre_run/post_run 触发）就绪；26 测试通过。剩余：resolver 接 PermissionEngine、行为引导规则、tool 级 hooks、数字人 requires 扩展——留 E2.5/E4。

---

### 批次 E3：Commands + Subagents ✅

**Commands —— 可复用的 slash 命令**（已做）

创建可重用命令：通过 `/name` 调用的可复用提示，用于在团队中标准化常见工作流。一个 command = `{name, prompt_template, allowed_tools?, description}`。`coworker/commands/`（新建，镜像 `coworker/skills/base.py`）。
- **CommandLoader** 扫 `state_dir()/commands/<name>/COMMAND.md`（YAML frontmatter `name/description/allowed-tools` + markdown body 即 `prompt_template`）；`command_catalog_text` 注入可用命令目录到 agent instructions（信息性，命令不是 agent 工具）。
- **Composer `/` 自动补全**（前端，`Composer.tsx`）：输入 `/`（行首或空格后）触发下拉浮层（命令名 + 描述），↑↓ 键盘导航、Enter/Tab 选中、Esc 关闭、失焦关闭。选中后调 `GET /v1/commands/{name}` 拿 `prompt_template`，把 `/query` 替换为模板文本（含 `{selection}`/`{file}`/`{input}` 占位符——直接插入，用户手填）。选中即展开模板，后端无需拦截。
- **`/v1/commands` 只读路由**（`GET /v1/commands` 列表 + `GET /v1/commands/{name}` 完整模板）。命令文件手写或 E4 plugin 打包安装，本期不做创建/编辑 UI。
- **CommandsPanel**（`CustomizeView.tsx`）：Commands tab 从 ComingSoon 升级为命令列表（`/name` + description + allowed_tools chips + 查看模板 modal）。搜索过滤 + 计数 badge。
- **数字人专属 command**（spec `commands` 字段）+ **团队标准化**（command 随 persona/plugin 打包分发）：留 E4。

**Subagents —— 委托子代理**（部分已做：泛化委托；内置子代理/自定义子代理 UI 留 E3.5/E4）

将工作委托给子代理：子代理是专门的助手，它们在**自己的上下文窗口**中运行，以便主代理可以**并行化操作**并保持专注。openworker 的 Persona 系统已是 subagent 的天然载体（`PersonaManifest` 已有 `tools/skills/mcp`，`to_agent()` 已能组装 runtime Agent），缺的是"委托"这一运行时动作。

已补：
- **`delegate_to_subagent(persona_id, task)` 工具**（`coworker/tools/subagent.py`，泛化既有 `explore` 模式）：主 agent 调用此工具，把子任务委派给指定 persona 执行。`build_subagent_engine(persona_agent, ...)` 用 persona 的 `tool_factory` 组装子引擎，PLAN 模式 + 无 approver（低 risk 可并行），**子 registry 过滤掉 `delegate_to_subagent`（无递归，仿 explore 的 "no explore in child"）**。`asyncio.run` 跑到完成，只返回 `{"report": ...}`。`delegate_tools(persona_registry, ...)` 工厂；`agent.py:build_engine` 加 `persona_registry` 参数，code family + 有 workspace 时注册。manager 两个 `build_engine` 调用点传 `persona_registry=self.personas`。`risk_level="low" + requires_approval=False` → 引擎 `asyncio.gather` 并行多个委托。
- **explore 子代理保留不变**（现有测试不回归）；delegate 是其泛化版（任意 persona，不只只读探索）。

待做（留 E3.5/E4）：
- **并行化**：已在 metadata 层就绪（低 risk），实际并行依赖主代理在一条消息里发多个 `delegate_to_subagent` 调用——引擎已支持。
- **前台/后台**：当前都是前台（阻塞到返回）。后台子代理（立即返回、独立工作）留 E3.5。
- **内置子代理**（Bash/Browser）：explore（只读）+ delegate（任意 persona，PLAN 模式）已覆盖核心价值。Bash/Browser 内置子代理作 E3.5 可选——需放开 PLAN 模式按 persona `default_permission_mode` 跑写入。
- **自定义子代理创建 UI**（Customize ▸ Subagents 新建，复用 persona 创建 UI）：留 E4。
- **Persona 市场源**（`persona_sources`，复用 Extension Hub）：留 E4。
- **数字人联动**（spec `subagents` 声明可委托 persona）：留 E4。

**E3 完成度**：Commands（加载 + `/` 补全 + 只读路由 + CommandsPanel）+ Subagents（泛化 `delegate_to_subagent` + 引擎接入）就绪；57 新/回归测试通过，tsc 0 错，浏览器端到端验证通过（Composer `/` 弹列表→选中展开模板；Commands tab 列表+查看模板 modal）。剩余：Bash/Browser 内置子代理、前台/后台、自定义子代理 UI、persona 市场源、数字人 requires——留 E3.5/E4。

---

### 批次 E4：统一 Extension Hub + Plugin 打包

**为什么放这里**：前面 E1-E3 把各扩展点做齐后，这批做"统一壳 + 跨类打包"。Customize 页面的壳已就位（7 大类 + 搜索 + Browse Marketplace），这批补各类的市场安装链路 + Plugin 跨类打包。

**✅ 完成（2026-07-31）**：

- **Plugin 市场安装链路**：落地策略 = 独立 `state_dir()/plugins/<name>/` 目录 + 注册表（`coworker/plugins/` 包：`sources.py`/`registry.py`/`installer.py`/`__init__.py`，镜像 skills）。默认源 `claude-official` → `https://github.com/anthropics/claude-plugins-official.git`，开箱即用。
- **marketplace.json 解析**：读 `.claude-plugin/marketplace.json` 的 `plugins[]`，每项 `name/description/category/source`。**4 种 source 全支持**：`git-subdir`（url+path+ref+sha）、`url`（整仓库+sha）、`github`（repo+commit+sha）、本地字符串（`./plugins/xxx`，相对 marketplace 仓库）。
- **分发层与执行层正交**：Plugin 是分发容器，整体落 `plugins/<name>/`；内部子 skill/command 保持原子，**复用现有 SkillLoader/CommandLoader 认领**（loader 代码零改动，只改 manager 传入的 dirs 列表：`SkillLoader([state_dir()/"skills", *plugins/*/skills])`）。装一个含 skills 的插件后 `/v1/skills` 立即出现该插件的 skill，卸载后消失。
- **MCP 注册解耦**：installer 通过 `mcp_register`/`mcp_unregister` 回调注册/反注册插件 `plugin.json` 的 `mcpServers`，不直接依赖 manager。卸载时按注册表记录的 server 名反向清理。
- **源管理 CRUD**：Customize ▸ 浏览市场 ▸ Plugins tab 内联源管理（添加/toggle/删除，内置不可删只可禁），镜像 DhpSourcesSection。**分类筛选条**（"全部" + 各 category chip）。
- **已装面板**：Customize ▸ Plugins tab = `PluginsPanel`（name + version + components chips：skills/commands/mcps 计数 + 卸载 + 更新按钮 + 更新检查）。
- **数字人 spec.requires 统一**（核心规则 2）：`DigitalHumanSpec` 加 `requires_plugins/commands/subagents`（`PluginDependency`/`CommandDependency`/`SubagentDependency` dataclass）。`requires` 从 2 元组扩展到 5 元组（mcps/skills/plugins/commands/subagents）。`to_dict` requires 键含全部 5 类。
- **健康面板扩展**：`dhp_plugins_health`/`dhp_commands_health`/`dhp_subagents_health`（照搬 `dhp_skills_health` 模板）。DhEditPanel 加 Plugins 健康块（缺失项旁「安装」按钮，一键从默认市场源补装）+ Commands 健康块（只读）+ Subagents 健康块（只读，市场源留 E5）。
- **一键补装**：`install_digital_human` 安装前检查 `requires_plugins` 缺失项，自动从默认市场源补装。
- **sha pin + 更新检查**：`_git_clone_or_pull` 支持 sha pin（`git fetch --depth 1 origin <sha>` + checkout，unshallow fallback）。`check_updates` 对比注册表记录 sha 与 marketplace 最新 sha，返回可更新项。

**验证**：15 新插件测试 + 1 新数字人测试通过；101 通过/1 预存环境失败（DHP repo 未克隆，非 E4 引入）；tsc 0 错；后端端到端：真实 marketplace catalog 解析正确、装 42crunch 插件后 5 个 skill 立即出现在 `/v1/skills`（loader 联动）、更新检查 sha 匹配、卸载清理干净。**注**：GUI 端到端无法在独立浏览器验证（Wails runtime 404→React 不 mount，仓库预存限制，非 E4 引入），前端代码 tsc 通过且遵循既有模式。

**不做（留 E5/后续）**：
- ~~Persona 市场源（`persona_sources`）——subagents 健康检查只读展示，不自动补装。~~ **✅ 已完成（E4 后续，见下）**
- Commands 市场源——commands 随 plugin 打包安装，无独立市场源。
- 插件依赖解析（plugin.json `dependencies` 一个插件依赖另一个插件）——留后续。
- 插件创建/打包 UI（本批只做消费端安装）。

---

### 批次 E5：浏览器登录态  ✅ 完成

- `browser_login` 从声明层升级为运行时：Playwright `storage_state` 持久化（`state_dir()/browser_profiles/<id>/storageState.json`），数字人复用登录态。`/v1/browser/logins` CRUD + 手动登录引导 UI。
- **登录引导双路径**：Playwright headed 优先（开窗口让用户手动登录，点「我已登录」后 `context.storage_state()` 落盘）+ cookie 粘贴回退（本机未装 Playwright 时前端自动切换到粘贴 cookie JSON 模式）。
- **运行时注入**：`browser_open_url` 工具开 URL 前查登录态注册表，URL host 命中某已存登录态时用 `storage_state` 建 context，agent 无感复用（`_BrowserController.page()` 接受可选 `storage_state` 参数；解耦的 `_login_state_resolver` 回调模式，manager 拥有注册表）。
- **UI**：CustomizeView 新增第 8 tab「登录态」管理已存登录态（添加/重新登录/删除）+ DhEditPanel 登录健康块（每项显示已登录/待登录 + 一键「登录」按钮，预填 url/label 打开 `BrowserLoginModal`）。
- 数字人 spec `requires.browser_login` 声明它需要哪个站点的登录态，`dhp_logins_health` 健康检查 + DhEditPanel 引导登录。
- **安全约束不变**：ilink 红线不动。本批只做通用网站的登录态持久化（Playwright storageState + cookie），不涉及任何特定平台逆向。
- **测试**：`tests/test_browser_logins.py` 19 项（Registry CRUD + slug 化 + path traversal 防护 + cookie 校验落盘 + Playwright mock + host 匹配）。tsc 0 错。浏览器端到端验证通过（Logins tab cookie 粘贴完整流程 + DhEditPanel 健康块渲染）。

**不做（留后续）**：登录态过期检测（cookie expiry 主动提醒）、登录态导出/导入（备份迁移）、多 profile per site（一站点多账号）、Playwright `user_data_dir` 持久化模式（比 storageState 更全，含 IndexedDB）。

---

### 批次 E4 后续：Persona 市场源  ✅ 完成（2026-08-01）

E4 完成时把 Persona/Commands 市场源留到了 E5；E5 落地后补齐 Persona 市场源（Commands 随 plugin 打包安装，无独立市场源——保持不做）。

- **Persona 市场源**（`persona_sources`）：镜像 plugin/skill 源管理。`coworker/personas/sources.py`（`PersonaSource` + `PersonaSourceManager`，prefs 键 `persona_sources`）。**无内置源**（无 Anthropic 官方 persona 市场仓，用户自加；空仓守护仍生效——日后加内置源即不可删只可禁）。
- **marketplace.py**：`list_catalog(source, cache_root)` clone 仓库→递归扫描 `*.md`→解析 frontmatter 元数据（lenient，目录展示用，跳过 README/无 frontmatter/无 body 的非 persona md）→返回 `[{id,name,tagline,description,icon,family,file}]`。`install_persona(source, persona_id, registry, cache_root)` 定位该 persona 的 md→拷入临时目录→复用 `PersonaRegistry.install_from_dir`（**只装选中的那一个，不装兄弟文件**；复用 consent 摘要 + snapshot + disabled-pending-approval 生命周期，市场源不改信任模型）。
- **git clone TTL**：`_git_clone_or_pull` 自带 10 分钟缓存刷新（镜像 skills installer），重复浏览目录不重复 clone。
- **后端路由**：`GET/POST/PATCH/DELETE /v1/personas/sources`、`GET /v1/personas/sources/{id}/catalog`、`POST /v1/personas/sources/{id}/install`。
- **前端**：`CustomizeMarketplace` 的 Subagents tab 从占位文案升级为 `PersonaBrowseSection`（源选择器 + 内联源 CRUD + 添加源表单 + catalog 列表 + 安装按钮，镜像 PluginBrowseSection；persona 项显示 id/name/tagline/family chip）。`api.ts` 加 `PersonaSource`/`PersonaCatalogItem` + 6 函数。i18n 加 `persona_marketplace_no_source` 空状态文案。
- **测试**：`tests/test_persona_sources.py` 12 项（SourceManager CRUD + ensure_builtins 空操作 + list_catalog 发现所有 persona/跳过非 persona + 元数据 + install 落地 disabled-pending-consent + 只装选中一个 + subfolder manifest 安装 + 不存在报错 + 卸载）。120 测试全过（含 persona/plugins/browser_logins/skills 回归）。tsc 0 错。浏览器端到端：Subagents tab 空状态文案 + 添加源表单 + 源出现在选择器 + catalog clone 错误路径全验证。

## 不做 / 后续

- **ilink 个人微信逆向协议**：红线，不碰。微信集成只走合规路径：企业微信自建应用 API、ClawBot 官方腾讯插件。
- **自建 MCP 服务器开发框架**：openworker 是 MCP 消费者，不做"用 openworker 开发 MCP server"方向。
- **插件代码执行沙箱**：插件本质是声明 + 资源（SKILL.md/persona/spec.yaml/command 模板），不跑任意代码。需执行的场景走 MCP（已有 sandbox 意识）。

## 优先级

**当前选定顺序**（用户确认）：

1. **E1**（Skills 全链路）— 最高性价比，skills 半成品只差安装链路。**← 进行中**
2. **E2**（Rules 三态 + Hooks 雏形）— 安全/合规 + 数字人运行前后钩子。**✅ 完成（CRUD+UI+触发；resolver 接引擎/数字人 requires 留 E2.5）**
3. **E3**（Commands + Subagents）— 委托子代理是数字人"并行干活"的关键能力。**✅ 完成（Commands 全链路 + delegate_to_subagent 泛化委托；Bash/Browser 内置子代理、创建 UI、市场源留 E3.5/E4）**
4. **E4**（统一 Hub + Plugin 打包）— 跨类打包 + 自定义源（含 claude-plugins-official.git）。**✅ 完成（Plugin 市场安装链路 + 4 种 source + loader 联动 + spec.requires 统一 + 健康面板扩展 + 一键补装；Persona/Commands 市场源留 E5）**
5. **E5**（浏览器登录态）— 让数字人复用已登录的站点。**✅ 完成（Playwright storageState + cookie 粘贴双路径 + browser_open_url 自动注入 + Customize Logins tab + DhEditPanel 健康块 + 19 测试）**

## 各批次对应的 Customize 类别就绪度

| 类别 | 当前进度 | 哪批补齐 |
|---|---|---|
| MCPs | ✅ 真实数据（McpRow 复用）| E4 补市场安装 |
| Skills | ✅ 真实数据（列表 + install/uninstall + 市场浏览）| E1 完成 |
| Subagents | ✅ 真实数据（Persona 列表 + toggle + 删除 + `delegate_to_subagent` 泛化委托已接引擎）| E3 完成（Bash/Browser 内置子代理、创建 UI、市场源留 E3.5/E4）|
| Plugins | ✅ 真实数据（PluginBrowseSection 市场+分类筛选+安装 + PluginsPanel 已装+卸载+更新）| E4 完成 |
| Rules | ✅ 真实数据（CRUD + allow/deny/ask 三态 UI + 引擎接入）| E2 完成（数字人 requires_rules 留 E4）|
| Commands | ✅ 真实数据（加载 + Composer `/` 补全 + CommandsPanel + 只读路由）| E3 完成（创建/编辑 UI、数字人 commands 留 E4）|
| Hooks | ✅ 真实数据（CRUD + pre_run/post_run + enabled toggle）| E2 完成（tool 级 hooks 留 E2.5）|
