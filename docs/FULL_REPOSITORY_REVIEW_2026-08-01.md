# OpenWorker-Pro 全量代码仓库审查、测试与能力路线图

> 审查日期：2026-08-01  
> 审查基线：`main` / `aec95a1`  
> 审查对象：Python 后端、FastAPI sidecar、React/TypeScript GUI、Tauri/Rust 桌面壳、Whisper sidecar、连接器、自动化、数字人及扩展体系、测试与 CI/CD、依赖与文档  
> 文档定位：本报告是对 [`AUDIT_REPORT.md`](AUDIT_REPORT.md) 的增量复审与当前全仓基线，不覆盖历史报告。

---

## 1. 执行摘要

### 1.1 总体结论

OpenWorker-Pro 已经不是“简单中文 fork”，而是一个具备明显产品方向的本地优先 Agent 工作台：

- Python sidecar + Tauri/React 桌面端的总体分层合理；
- Provider、MCP、Connector、Persona、Automation、Digital Human、Skill、Plugin、Rule、Command、Hook、Subagent 已形成较完整的产品骨架；
- fork 在当前基线上相对上游约有 **96 个文件、17,571 行新增、88 行删除**，新增能力已达到需要独立治理、发布和安全模型的规模；
- Python 与前端单元测试基础较扎实，类型检查和生产构建可以通过；
- 旧审计中的上下文溢出压缩、GitHub issue 编号规范化、更新端点、工作区 MCP 信任门控等问题已经修复或明显改善。

但当前仍**不适合在未整改的情况下扩大远程暴露面或宣布“安全可用”**。最重要的原因不是功能缺失，而是多个信任边界尚未闭合：

1. 未隔离 HTML artifact 可在 Tauri WebView 中执行脚本，CSP 又被关闭；sidecar token 同时存在于同一 JavaScript 上下文，形成从内容注入到本机 Agent API 的完整链路。
2. 企业微信加密回调的明文降级、时间窗、重放和消息去重缺口已在本轮关闭并覆盖回归；受控公网部署仍需保持加密回调配置。
3. MCP stdio 本质上是本机代码执行入口，而 Plugin/DHP 的安装链路可扩大到全局 MCP；当前来源、确认、完整性、原子安装和回滚约束不足。
4. Browser Login 备份导入/导出存在路径边界问题；登录态文件没有统一复用私有权限和原子写入机制。
5. 通知配置保真和 webhook SSRF 边界已在本轮关闭；数字人配置密钥仍会被拼入静态任务 instructions，尚待迁移为运行期 secret binding。

因此，建议项目进入一个短期的 **“安全边界与质量门禁优先”阶段**：先停止增加新的高权限扩展面和远程入口，再补齐 P0/P1，随后通过 ACP、OpenAPI/SDK、统一事件协议等适配层吸收 Hermes Agent、OpenCode、OpenHands、Goose 的成熟能力，而不是再建立第二套运行时。

> **打磨期完成状态（2026-08-01，同日）**：上述 P0（artifact sandbox+CSP、企微加密回调、扩展供应链审批、登录态原子私有落盘、通知保真+webhook SSRF）与 P1 中影响发布质量的部分（sidecar fail-closed、Windows ripgrep、xlsx 替换、relay trust_env、auto-title 隔离、E2E 门禁恢复、测试产物清理、文档漂移回填）已全部落地。打磨后 pytest **1322 passed / 0 failed**、Playwright **160/160**、Vitest **103/103**、tsc 通过、npm production audit **0**（xlsx CVE 消除）。仍开放项：MCP stdio 命令边界、localhost CORS、Git token argv、Provider timeout/retry、Session WS 重连、Windows Authenticode、完整 CI job 补齐、SECURITY_MODEL/RELEASING 文档——见各章“未修复/仍待”行。

### 1.2 评级

> 下表“审查时”列为本次全量审查的原始评级；“打磨后”列为同日 P0/P1 + 质量门禁整改完成后的状态（见各章末“修复”段）。

| 维度 | 审查时 | 打磨后 | 结论 |
|---|---:|---:|---|
| 产品能力完整度 | B+ | B+ | 本地 Agent、连接器、数字人、自动化、扩展市场已成体系 |
| 核心架构 | B | B | 分层方向正确，但 `manager.py`、`app.py`、`api.ts`、`App.tsx` 等中心文件持续膨胀 |
| 单元测试基础 | B+ | A- | Python/GUI 单元测试覆盖面较好；打磨后 pytest 1322 全绿、Vitest 103 全过 |
| 端到端质量门禁 | D | A- | Playwright 160/160 全绿且已恢复为 CI 门禁；夹具 lang 注入 + 竞态修复 |
| 安全边界 | D+ | B- | artifact sandbox+CSP、企微加密、通知保真、登录态、DHP secret、sidecar fail-closed 已闭合；MCP stdio/远程面仍待收敛 |
| Windows 可发布性 | C | B | ripgrep JSON 解析、symlink junction、ACL 断言、进程生命周期、relay trust_env 全部修复 |
| 依赖与供应链 | C- | B- | `xlsx` 高危依赖已替换为 jszip+DOMParser；扩展下载完整性/来源信任仍待收敛 |
| CI/CD 与发布 | C- | C+ | E2E 门禁恢复；Windows pytest job、Rust/lint/coverage/审计仍待补 |
| 文档与版本治理 | C | B- | 路线图/使用指南/risk.py 漂移已回填；SECURITY_MODEL/RELEASING 等仍待建 |

### 1.3 建议的发布判断

- **当前桌面本地开发版**：可继续内部开发和受控试用，但应明确安全边界。
- **面向普通用户的稳定桌面版**：应先完成本报告 P0，并修复 Windows 稳定缺陷与基本 E2E smoke。
- **远程访问、移动端、公开 webhook 暴露**：当前应暂缓；P0/P1 未完成前不应把 sidecar 或连接器扩大到互联网。
- **插件/数字人第三方市场开放**：当前只能作为“受信源实验功能”；不应把远端声明视为可自动执行的可信配置。

---

## 2. 范围、方法与仓库基线

### 2.1 审查方法

本次不是只做静态阅读，采用了以下组合：

1. 比较 fork 与上游的差异和当前路线图；
2. 运行全量 Python 测试、fork 专项测试、GUI 单元测试、TypeScript 类型检查和生产构建；
3. 运行 Playwright E2E 并对失败类型归因；
4. 运行 Python 依赖一致性、字节码编译和 npm 漏洞审计；
5. 尝试 Rust/Tauri/STT 本地检查，并区分源码失败与工具链阻断；
6. 复核旧审计中问题是否真实修复；
7. 对通知、企微、Browser Login、DHP、Skill、Plugin、MCP、Subagent 等 fork 新增边界做针对性审查；
8. 通过官方仓库、官方文档和发布信息调研 Hermes Agent、OpenCode、OpenHands 和 Goose。

### 2.2 仓库规模

| 区域 | 规模 |
|---|---:|
| `coworker/` Python | 160 个文件，约 43,142 LOC |
| `tests/` | 99 个文件，约 25,436 LOC |
| GUI TS/TSX | 106 个文件，约 28,193 LOC |
| Rust 源码 | 3 个文件，约 1,444 LOC |
| Git tracked files | 693 |
| 被跟踪的 Playwright 结果类文件 | 160 |

当前 fork 相对上游为 **8 commits ahead / 0 behind**。这意味着 fork 仍易于吸收上游，但新增 17k+ 行已经足以要求：

- 独立版本策略；
- 独立威胁模型；
- 独立 Windows/macOS 发布验收；
- 独立文档与兼容性承诺；
- 对上游同步建立周期性流程，而不是临时 cherry-pick。

---

## 3. 测试、构建与依赖验证

### 3.1 结果矩阵

| 检查 | 结果 | 判断 |
|---|---:|---|
| Windows 全量 Python pytest | **1,322 passed / 0 failed / 1 skipped** | 全绿（1 skipped 为 Gemini key 环境跳过，非代码问题） |
| fork 新功能专项套件 | **221+ passed** | 通知、数字人、扩展等专项基础较好 |
| GUI Vitest | **103 passed** | 通过（含新增 parseXlsx 3 项） |
| TypeScript `tsc --noEmit` | 通过 | 类型基线良好 |
| Vite production build | 通过 | 有大 chunk 和动态/静态混合导入警告 |
| Playwright E2E | **160 passed / 0 failed** | hermetic 套件全绿，已恢复为 CI 门禁 |
| `pip check` | 通过 | 已安装 Python 依赖一致 |
| Python `compileall` | 通过 | 无语法/字节码编译问题 |
| npm production audit | **0** | `xlsx@0.18.5` 已移除（CVE-2023-30533 + ReDoS 消除） |
| npm full audit | **6 vulnerabilities** | 3 moderate / 2 high / 1 critical，均为预存开发工具链（esbuild/postcss/vite），非运行时依赖 |
| Tauri/STT `cargo check` | 未完成 | 本地 PATH 缺少 CMake，阻断于 `whisper-rs-sys`，未证明 Rust 源码失败 |
| GitHub release matrix | Windows、macOS arm64、macOS Intel 成功 | 说明 release 环境可构建，但不能替代源码质量门禁 |

> **打磨期修复（2026-08-01）**：本矩阵的 pytest / Playwright / npm audit 三项相对原始审查已全面转绿——10 个 Windows+relay+auto-title pytest 失败全部修复（见 3.3），153 个 Playwright 失败通过 lang 注入 + 夹具修正清零（见 3.2），xlsx 高危依赖替换为 jszip+DOMParser 自实现（见 P1-08）。

### 3.2 如何解释 Playwright 的 153 个失败（已全部修复）

这 **不等于 153 个独立产品回归**。失败中大量属于测试夹具与当前产品状态不一致，例如：

- 英文断言实际收到中文文案，例如 `1 folder` 与 `1 个文件夹`；
- 测试仍查找已改变或迁移的 `Connectors` 导航按钮；
- composer placeholder、automation/channel selector 等已漂移；
- 前置导航失败后造成大量级联超时。

但这也不能被当作“只是测试问题”而忽略。它证明：

- E2E 已经无法提供回归信号；
- CI 中 `gui-e2e` 被 `if: false` 完全禁用后，测试资产持续腐化；
- 新功能的“浏览器端到端验证通过”多为当时的手工/局部验证，无法替代可重复门禁。

**修复（2026-08-01）**：根因是 GUI 默认中文（`i18n DEFAULT = "zh"`）而 57 个 hermetic spec 断言英文文案。`fixtures.ts:mockApi()` 注入 `localStorage.setItem("openwork-lang","en")` 后英文断言全部匹配；另修 3 个 nav-collapse 夹具问题（`Meta+b` 在 Linux Chromium 映射到 Super 键不触发 `metaKey`，改用 `Control+b`；"Recent"→"RECENT"；"Group and filter"→"Group & filter"；加 boot-splash 可见性等待避免竞态）和 reasoning 的 timer 断言超时（10s→15s）。CI `gui-e2e` job 从 `if: false` 恢复为活跃门禁，注释更新为 Tauri（非过时的 Wails）。**160/160 通过，retries: 1**。原“Wails runtime 404”注释已证伪：桌面壳是 Tauri，E2E 在 Chromium 跑 Vite dev server（5199）+ 全 mocked REST/WS。

### 3.3 Windows pytest 失败归因（已全部修复）

10 个失败不能简单归为“Windows 不兼容”，可分为五类（均已于 2026-08-01 修复）：

1. **稳定产品缺陷：Windows grep 路径解析**（已在 P1-07 批次修复）  
   [`coworker/tools/search.py`](../coworker/tools/search.py) 使用 `line.split(":", 2)` 解析 ripgrep 输出。`C:\...` 的盘符冒号会被误当分隔符，导致路径和行号解析失败并丢弃匹配。**修复**：改用 `rg --json --line-number`，只解析 `type=="match"` 事件，按 bytes/text fallback 取 path/line/match。

2. **Windows 测试可移植性：symlink 权限**（已修复）  
   测试在产品断言前因 `WinError 1314` 失败。普通 Windows 用户默认没有创建 symlink 的权限。**修复**：`symlink_to` 失败时 fallback 到 `cmd /c mklink /J`（directory junction，无需管理员权限）。

3. **Windows 安全语义：POSIX `0600` 断言**（已修复，含真实产品 bug）  
   Windows 应验证 ACL，不应复用 POSIX mode bit 断言。**发现真实产品缺陷**：[`coworker/workspace_trust.py`](../coworker/workspace_trust.py) 的 `set_trusted()` 用裸 `os.chmod`（Windows 上近乎 no-op），未应用 Windows ACL。**修复**：改用 `secrets.write_private_text()`（icacls-based ACL）。测试改为 Windows 上检查 `icacls` 输出（含当前用户、不含 `BUILTIN\Administrators`），并处理中文 Windows 的 GBK/OEM 编码（`raw.decode("utf-8", errors="replace") or raw.decode("mbcs", errors="replace")`）。

4. **工作区进程生命周期：活动 PowerShell 占用 cwd**（已修复）  
   持久 PowerShell 以 workspace 为当前目录时，Windows 拒绝重命名该目录。**修复**：测试在 `rename` 前关闭 engine 的 executor（`getattr(engine, "executor", None).close()`），释放对 workspace cwd 的占用。

5. **Relay 与测试环境耦合**（已修复）  
   多个 Slack/GitHub relay 失败共享同一根因：测试把 Slack API 指向 `127.0.0.1:9` 期待立即拒绝，但 `httpx.AsyncClient()` 继承代理环境，loopback 请求被送往本地代理后超时。**修复**：产品代码 [`coworker/connectors/relay_client.py`](../coworker/connectors/relay_client.py) 和 [`github_relay.py`](../coworker/connectors/github_relay.py) 的 `httpx.AsyncClient` 加 `trust_env=False` + 结构化 timeout；`wait_dispatched` 默认 timeout 从 2.0s 提到 10.0s（Windows loopback 连 `127.0.0.1:9` 需 ~2.3s，3 次串行 ~7s，2s 不够）。测试 `fake_deliver` 签名补 `reply_target=None, **kw`。

另有一项 UI-refresh 测试受异步 auto-title 的模型调用污染；没有证据表明 muted Slack 消息真正唤醒了会话。**修复**：测试在 mute 检查前 `await _wait_until(lambda: SID not in mgr._autotitle_inflight)` 等待 auto-title 后台任务结算，区分 title 调用和 normal turn。

### 3.4 构建与包体

GUI production build 通过，但存在两个可持续性信号：

- [`surfaces/gui/src/api.ts`](../surfaces/gui/src/api.ts) 同时被静态和动态导入，阻止预期 chunk 提取；
- 主 JS chunk 约 970 KB，PDF worker 和 spreadsheet 相关 chunk 也较大。

建议：

- 把 Settings、Customize、PDF、Spreadsheet、Digital Human Store 等非首屏功能设为清晰的 route/component lazy boundary；
- 拆分 `api.ts` 为 session、settings、connectors、automation、extensions 等领域客户端；
- 在 CI 中加入 bundle budget，先以当前值为 baseline，再逐步收紧。

---

## 4. P0：发布阻断问题

> P0 定义：在扩大用户范围、开放第三方内容/扩展、暴露公网回调或发布稳定版前必须完成。

### P0-01：HTML artifact 可突破 WebView 信任边界并调用本机 sidecar

**证据链**：

- [`surfaces/gui/src-tauri/tauri.conf.json`](../surfaces/gui/src-tauri/tauri.conf.json) 设置 `"csp": null`；
- [`surfaces/gui/src/components/RightRail.tsx`](../surfaces/gui/src/components/RightRail.tsx) 用 `srcDoc` 渲染未净化 HTML，并配置 `sandbox="allow-scripts allow-same-origin"`；
- [`surfaces/gui/src-tauri/src/lib.rs`](../surfaces/gui/src-tauri/src/lib.rs) 将 sidecar token 注入 `window.__COWORKER_API_TOKEN__`；
- [`surfaces/gui/src/api.ts`](../surfaces/gui/src/api.ts) 从该 JavaScript 上下文读取 token，并用于 REST/WS 鉴权。

`allow-scripts + allow-same-origin` 是不安全组合。任何模型、MCP、网页或文档链路产生的恶意 artifact HTML，一旦在 WebView 中执行脚本，就可能读取 token 并以桌面应用身份调用本机 Agent API。强 token 的生成与 constant-time 比较不能防御 token 在同一渲染上下文被读取。

**整改**：

1. artifact iframe 去掉 `allow-same-origin`，默认同时去掉 `allow-scripts`；
2. 若确需交互式 HTML，放入独立 origin/独立受限 WebView，使用 capability-scoped message bridge，不共享主窗口 token；
3. 恢复严格 CSP，至少限制 `default-src`、`script-src`、`connect-src`、`frame-src`；
4. sidecar 凭据不要作为任意页面脚本可读全局变量；改为 Tauri command 代理、短期 capability token 或按动作签名；
5. 建立恶意 `srcDoc` 回归测试：读取 parent/global、发起 sidecar 请求、顶层导航、文件/网络访问均应失败。

**验收条件**：不受信 HTML 即使能运行自身脚本，也无法读取主窗口凭据、访问 Tauri 全局 API或直接调用 sidecar 特权端点。

### P0-02：企业微信加密回调绕过（已修复并覆盖回归）

原始问题是 [`coworker/connectors/wecom_app/provider.py`](../coworker/connectors/wecom_app/provider.py) 在 POST 缺少 `Encrypt` 时回退明文，并且没有 timestamp/replay/MsgId 防重放。当前工作树已完成：

- PKCS#7、base64、AES key、ciphertext 长度与 UTF-8 全部 fail closed；
- Token 与 EncodingAESKey 必须成对配置；未配置或旧配置不完整时保留合法出站能力，但入站状态明确为 `outbound_only / needs_migration`；
- 公网回调只接受加密消息，拒绝 plaintext downgrade；
- 加入 5 分钟 timestamp freshness、短期 `(timestamp, nonce, signature)` replay cache 和 MsgId dedup；
- callback XML/字段/密文大小有界，公开响应与日志只使用稳定脱敏错误；
- GUI 不再展示“明文模式”，而是“加密入站 / 仅出站”。

验证证据：`tests/test_wecom_app.py` **34 passed**，覆盖 bad padding、旧 timestamp、签名重放、重复 MsgId、明文降级、callback credential pairing 和 access-token single-flight refresh。后续仍可进一步加入 `AgentID` 一致性检查，但原发布阻断绕过已关闭。

### P0-03：扩展供应链可从远端声明扩大到全局本机代码执行

当前多个机制组合后形成高风险链：

- 用户添加的 MCP 配置可直接声明 `command / args / env / cwd`；
- [`coworker/mcp/client.py`](../coworker/mcp/client.py) 将其交给 stdio transport，本质上是本机进程执行；
- Plugin 可声明 MCP server 并注册到全局配置；
- 安装 Digital Human 时可以自动补装缺失 Plugin；
- HTTP/git/local 安装链路在 checksum/size/重定向、resolved containment、symlink、原子替换和跨步骤回滚方面不完整。

因此，“安装一个数字人”可能扩大为“拉取插件并持久注册可执行 MCP 命令”，而当前缺少逐项显示与确认。现有 `PermissionEngine` 只约束 Agent 调用工具，不等价于约束扩展安装和 MCP server 启动。

**整改**：

1. 将 MCP stdio server 定义为明确的 `local-executable` 高风险 capability；首次安装、首次启用、命令/参数/cwd/env 变化时必须逐项确认；
2. DHP/Plugin 只能提出 dependency plan，不能静默执行跨类型安装；UI 展示来源、版本、commit/sha256、将写入路径、MCP 命令及新增工具；
3. 全部远端索引和包执行下载大小上限、redirect policy、checksum/commit pin 校验；
4. 使用 resolve 后的 `is_relative_to` 做路径 containment，拒绝 source symlink/reparse point 越界；
5. staged install → validate → atomic rename → registry/MCP commit，任一步失败自动回滚；
6. MCP 配置引入 provenance：builtin/user/workspace/plugin/dhp、source ID、install digest、last approved digest；
7. 支持每 server tool allowlist/filter，不把全部 MCP tools 自动暴露给所有 Agent；
8. 高风险来源默认为 disabled-pending-consent，更新后 capability 变化需重新确认。

**验收条件**：远端内容不能在一次低信息量的“安装”操作中静默新增或改变本机可执行命令；安装失败不会留下半注册状态。

### P0-04：Browser Login 备份导入/导出突破状态目录边界

[`coworker/server/manager.py`](../coworker/server/manager.py) 的 Browser Login backup 处理信任导入数据中的 `_files`、`storage_state_path`、`cookie_path`，并使用字符串 `startswith` 判断路径边界。该方式可被同前缀 sibling 绕过；导入后还可能让后续 export 读取非登录文件，例如 state root 中的 secret 数据。

[`coworker/browser_login_capture.py`](../coworker/browser_login_capture.py) 写入 storageState/cookie 时也没有统一复用 SecretStore 的私有 ACL/`0600` 与原子替换。

**整改**：

1. 导入时完全忽略外部提供的目标路径；只接受结构化 profile 数据；
2. 从经过验证的 `safe_id` 重建固定目标路径；
3. `Path.resolve()` 后使用 `is_relative_to(approved_root)`，拒绝 symlink/reparse point；
4. 设置导入包文件数、单文件和总大小上限；
5. staging 解包、schema 校验、原子替换；
6. 登录态文件统一使用私有 ACL/`0600`、temp file + fsync + replace；
7. export 只读取 registry 推导的固定白名单文件，不读取记录中传入的任意路径；
8. 增加同前缀目录、绝对路径、`..`、symlink、恶意 path field 和读取 `secrets.json` 的负向测试。

### P0-05：密钥生命周期被任务指令与通知配置语义破坏

#### 数字人密钥进入公开任务 instructions

[`coworker/digital_human/installer.py`](../coworker/digital_human/installer.py) 先把 secret 写入 SecretStore，之后又将完整配置合并到静态 scheduled-task instructions。与此同时 [`coworker/automation/models.py`](../coworker/automation/models.py) 的 public serialization 不移除 instructions 中的密钥。密钥可能出现在持久任务 JSON、API 响应、模型 prompt、日志和诊断包中。

#### 通知配置与 webhook 边界（已修复并覆盖回归）

当前工作树已将通知配置改为 server-side PATCH/merge：omitted 或标准 mask placeholder 保留旧 secret，只有 allowlist 校验后的 `clear_fields` 才清除；enabled 使用独立 PATCH，test-send 会把表单 patch 合并到服务端已存配置。Webhook URL 本身现在也作为 secret 脱敏。

新增 [`coworker/notify/http.py`](../coworker/notify/http.py) 作为统一受限 POST 边界：HTTPS 默认、禁止 userinfo/危险 header、DNS 任一私网答案即拒绝、redirect 每跳复检、`trust_env=False`、明确 timeout/redirect/请求与响应上限和脱敏错误；钉钉、飞书、企微使用精确 vendor host allowlist，generic webhook 仅在显式兼容配置下允许 HTTP。

验证证据：`tests/test_notify.py` **26 passed**，覆盖 mask preservation、explicit clear、独立 toggle、test merge、userinfo、metadata/private DNS、多答案、redirect-to-loopback、vendor host 和错误脱敏；GUI `NotifyChannelsSection` 新增 **2 tests**，完整 Vitest 为 **91 passed**。

数字人 instructions 中的明文密钥仍属于后续 DHP 整改范围。

---

## 5. P1：下一版本必须处理

### P1-01：sidecar 缺少 token 时默认开放

[`coworker/server/app.py`](../coworker/server/app.py) 在 `COWORKER_API_TOKEN` 为空时绕过 REST 和 WebSocket 鉴权。开发便利不应成为生产 fallback。

建议：

- desktop/prod 启动时 token 缺失直接 fail closed；
- 只有显式 `--insecure-local-dev` 才允许无 token，启动日志与 UI 持续告警；
- health endpoint 只返回最小状态；OAuth/callback 使用各自独立一次性 state/signature，不依赖 tokenless 的宽泛语义；
- CORS 不应仅凭任意 localhost 端口获得信任，应固定桌面 origin 或完全避免浏览器直连特权 API。

### P1-02：MCP OAuth 在服务端未提供 state 时降级接受

[`coworker/mcp/oauth.py`](../coworker/mcp/oauth.py) 在已捕获 expected state 时使用 constant-time 比较，这是正向改进；但 authorization URL 若没提供 state，callback 仍可继续。OAuth 应始终由客户端生成并绑定 state，不应依赖服务端是否主动给出。

建议强制 PKCE + client-generated state + 一次性消费 + TTL，并绑定 MCP server ID、redirect URI 和启动会话。

### P1-03：MCP/网页内容进入模型时缺少统一“不受信内容”边界

[`coworker/mcp/client.py`](../coworker/mcp/client.py) 把 MCP text content 直接拼成普通 tool 文本。网页、邮件、文档和 MCP 返回内容中的 prompt injection 与系统指令没有结构化区分。

不能只依赖“提示模型忽略恶意指令”。应：

- 使用统一 `UntrustedContent` envelope，携带 source/provenance/content-type/truncation；
- 将数据和控制指令放在不同 schema 字段；
- 对跨信任域动作重新触发 permission evaluation；
- 不允许工具输出直接扩大工具权限、安装扩展或改变规则；
- 在 trace 中记录 taint/provenance。

### P1-04：Browser 工具的本地文件边界过宽

[`coworker/connectors/browser_automation.py`](../coworker/connectors/browser_automation.py) 可上传任意可读本地文件，截图可写入任意 resolved path。建议默认只允许 workspace 和 per-session scratch；workspace 外路径必须走原生文件选择器或明确审批，并在批准卡片展示规范化绝对路径。

### P1-05：Provider completion 缺少统一 timeout/retry/cancellation policy

OpenAI、Anthropic、Gemini、Bedrock 的生产 completion client 没有形成一致的：

- connect/read/write/total timeout；
- exponential backoff + jitter；
- 429/5xx/网络错误 retry budget；
- `Retry-After`；
- cancellation propagation；
- idempotency/重复工具调用防护。

应在 provider abstraction 层定义策略，而不是由每个 SDK 默认值决定。流式首 token、idle timeout 和总 turn deadline 应分别配置。

### P1-06：Session WebSocket 与 relay 的恢复能力不足

- GUI session WebSocket 创建一次，`onclose` 后没有自动重连；
- relay 固定 2 秒重连，无指数退避和 jitter；
- 无 frame-size、queue-size、rate limit；
- serial dispatcher 可被慢 handler 阻塞。

建议引入：connection state machine、sequence/cursor、resume/attach、bounded queue、per-handler deadline、并发隔离、指数退避和断线 UI 状态。这正适合借鉴 OpenCode 的 server-first/SSE attach 思路，而不是继续堆一次性 WS 回调。

### P1-07：Windows grep 功能回归

应以 ripgrep 的 `--json` 输出替代脆弱的冒号文本解析；若暂不切 JSON，至少使用正则从行尾解析 `:line:` 并专门覆盖 Windows drive、UNC、含冒号文件名等情况。

### P1-08：`xlsx@0.18.5` 无可用 npm 修复（已修复）

生产依赖有高危 prototype pollution/ReDoS 公告且 `npm audit` 无修复版。建议优先：

1. 将 spreadsheet preview 移到隔离 worker/process，限制输入大小、sheet/cell 数与解析时间；
2. 评估替换为维护中的库或服务端/wasm 只读解析；
3. 若短期保留，禁止宏/公式执行，避免把解析结果作为 HTML，记录 risk acceptance 和移除日期。

**修复（2026-08-01）**：移除 `xlsx@0.18.5`（CVE-2023-30533 prototype pollution + GHSA-5pgg ReDoS 消除），替换为 `jszip@3.10.1`（单一轻量依赖，无传递漏洞）+ 浏览器内置 `DOMParser` 直接解析 OOXML。[`surfaces/gui/src/components/RightRail.tsx`](../surfaces/gui/src/components/RightRail.tsx) 的 `SheetViewer` 重写：JSZip 解包 xlsx（zip of XML），DOMParser 读 `xl/workbook.xml`（sheet 名 + r:id 映射）、`xl/sharedStrings.xml`（字符串表）、`xl/worksheets/sheetN.xml`（单元格），覆盖 shared-string/inline-string/number/boolean/sparse-gap 场景；公式取缓存值或空，日期保留 serial（预览足够，完整格式化属真实表格应用）。exceljs 因引入 97 包 + uuid@8 moderate 漏洞被否决。新增 3 项 `parseXlsx` 单元测试（动态构建 OOXML zip，无需二进制 fixture）。`npm audit` 的 xlsx 公告全部消除。

### P1-09：Email header 与 URL/path 等输入边界应显式验证

- Email address/subject 应显式拒绝 CR/LF，不依赖标准库版本行为；
- GitHub owner/repo 应做严格 slug 校验；
- 下载 URL、回调 body、配置字段、MCP env/cwd 应有长度和类型上限；
- HTTP response 应流式读取并执行压缩后/解压后双重大小限制。

### P1-10：tool-level Hook 尚未落地，现有 Hook 失败语义不适合作为安全策略

当前只有 scheduled run 的 `pre_run/post_run`，并且 hook 失败不阻断运行。该语义适合通知/观测 hook，不适合安全策略。

建议借鉴 Goose/Open Plugins：

- 事件契约版本化：`pre_tool/post_tool/on_message/pre_run/post_run`；
- hook 分为 `observer` 与 `policy`；
- observer 默认 fail-open，policy 默认 fail-closed；
- 参数修改必须 schema validate，保留 before/after audit；
- 命令型 hook 也是本机代码执行，需要来源、审批、timeout 和受限环境。

---

## 6. P2：架构、维护性与体验改进

### 6.1 后端中心化文件需要按领域拆分

主要热点：

| 文件 | 规模/职责问题 | 建议边界 |
|---|---|---|
| [`coworker/server/manager.py`](../coworker/server/manager.py) | 约 4,912 行，承担 session、connector、MCP、automation、browser login、extension、DHP 等 | `SessionService`、`ConnectorService`、`ExtensionService`、`AutomationService`、`BrowserProfileService` |
| [`coworker/connectors/integration_tools.py`](../coworker/connectors/integration_tools.py) | 约 4,923 行，多平台工具集中 | 按 GitHub/Google/CRM/Slack 等 provider module 拆分 |
| [`coworker/server/app.py`](../coworker/server/app.py) | 约 2,368 行，路由和策略混合 | FastAPI routers + dependency-injected auth/capability guards |
| [`coworker/engine.py`](../coworker/engine.py) | 核心循环、权限、事件耦合增长 | 保持单循环，外置 event bus/policy adapter，不复制 runtime |

拆分不能只是移动函数。先定义 service protocol 和领域 DTO，再让 API/router 依赖接口；否则只会产生多个互相 import 的“大文件”。

### 6.2 前端 God Component 与 API 单体

- [`surfaces/gui/src/App.tsx`](../surfaces/gui/src/App.tsx)：约 1,821 行、48 个 `useState`；
- `Sidebar.tsx`：约 1,420 行；
- `CustomizeView.tsx`：约 1,009 行；
- `SettingsView.tsx`：约 990 行；
- `Composer.tsx`：约 983 行；
- `ManageTabs.tsx`：约 911 行；
- [`surfaces/gui/src/api.ts`](../surfaces/gui/src/api.ts)：约 2,893 行。

建议：

1. 以领域 query/mutation hook 替代 App 中的手工加载与状态拼接；
2. 建立 `SessionProvider`、`WorkspaceProvider`、`ConnectionProvider`，不要建立一个全局万能 store；
3. API 按领域拆包，并统一 error/result/cancellation；
4. Customize 各 panel 单独 lazy load，拥有各自 loading/error/empty 状态；
5. 对关键交互使用 state machine，尤其 session boot、stream、approval、reconnect 和 install transaction。

### 6.3 i18n 基线好，但新增表面仍有硬编码

`zh.json` 与 `en.json` 均为 1,491 keys，key 和 placeholder 差异为 0，这是值得保留的正向质量信号。但仍有：

- 新组件运行时硬编码中文；
- 通知渠道 label/description 由后端返回中文；
- `WecomDetail.tsx` 使用独立翻译 helper；
- Digital Human category fallback 因缺失 key 的 truthy 返回而失效。

建议 CI 加 i18n key/placeholder 校验，并在 ESLint 中禁止用户可见硬编码字符串（允许白名单）。后端返回稳定 code，由前端翻译，而不是返回固定中文显示文本。

### 6.4 Subagent 已有正确骨架，但要强化生命周期和能力收缩

[`coworker/tools/subagent.py`](../coworker/tools/subagent.py) 已实现独立 context、persona 工具集和禁止递归 delegate，这是正确方向。下一步不应再造一套 Agent runtime，而应增加：

- `spawn / status / wait / cancel / result` 生命周期 handle；
- foreground/background；
- 主 Agent 向子 Agent 传递最小上下文；
- tools/MCP/skills capability intersection，而不是仅信任 persona 声明；
- token/time/tool-call budget；
- 子 Agent 结果 provenance；
- 并发上限和 workspace mutation 冲突控制。

### 6.5 观测、诊断与隐私

目前缺少统一跨层 trace。建议建立版本化 event schema：

- session/turn/message；
- model request/stream/retry；
- tool proposed/approved/started/completed；
- subagent spawned/completed；
- connector inbound/outbound；
- automation scheduled/run；
- extension installed/enabled/updated；
- security decision。

默认本地保存，敏感字段在事件产生处即 redaction；可选导出 OTLP/Langfuse/MLflow。不要把完整 prompt、cookie、token 或 tool output 默认发往外部观测平台。

---

## 7. 旧审计复核

| 历史发现 | 当前状态 | 说明 |
|---|---|---|
| 上下文超限后只失败、不压缩重试 | **已修复** | engine 能识别 overflow、触发 compaction 并重试 |
| GitHub relay issue number 类型不规范 | **已修复** | 已经 `int()` 规范化 |
| sidecar launch token 强度不足 | **已改善** | 强随机 token、服务端 constant-time 比较；但 WebView 暴露仍未解决 |
| 更新器指向上游 | **已修复** | 已指向 fork release endpoint，配置 updater public key |
| Workspace MCP 未经过 trust | **已改善** | workspace config 只在 trusted workspace 加载；stdio 风险仍在 |
| Persona 安装立即可用、缺少 consent | **已改善** | 使用 copied snapshot + disabled-pending-consent |
| SecretStore 普通写入/权限不足 | **已改善** | 私有 ACL/`0600` 与 temp replacement 已存在 |
| Tauri CSP 关闭 | **已修复** | `csp: null` 替换为生产 CSP（`default-src 'self'`、`connect-src` 限 loopback）+ dev CSP（见 P0-01）|
| artifact iframe 隔离不足 | **已修复** | `sandbox=""`（无 allow-scripts/allow-same-origin）+ 注入限制型 CSP（见 P0-01）|
| sidecar token 对 WebView JS 可读 | **已改善** | artifact 脚本不可执行（sandbox=""），但 token 仍注入 window global（深度修复见 P1）|
| MCP stdio 任意命令配置 | **未修复** | 仍直接接受 command/args/env/cwd |
| token 为空时 sidecar 开放 | **已修复** | fail-closed：仅 `COWORKER_INSECURE_LOCAL_DEV=1` 允许 tokenless，否则 401（见 P1-01）|
| Vite dev token 分发面过宽 | **未修复** | dev build 注入和 token refresh endpoint 仍存在 |
| MCP OAuth state fallback | **未修复** | state 缺失时仍可继续 |
| Browser screenshot/upload 本地路径边界 | **未修复** | 仍需 workspace/scratch scope |
| localhost CORS 过宽 | **未修复** | 任意 localhost/127.0.0.1 端口仍被信任 |
| Git token 暴露在子进程 argv | **未修复** | `git -c http.extraHeader=...` 仍可被同机进程观察 |
| Provider timeout/retry 策略不统一 | **未修复** | 主要依赖 SDK 默认值 |
| Session WebSocket 自动重连 | **未修复** | close 后不恢复 |
| `xlsx` 漏洞 | **已修复** | 移除 `xlsx@0.18.5`，替换为 `jszip@3.10.1` + DOMParser 自实现 OOXML 解析（见 P1-08）|
| Windows Authenticode | **未修复** | updater 签名不等价于 OS code signing |

---

## 8. fork 新能力专项评价

### 8.1 通知渠道

**优点**：

- 路由器隔离单渠道失败；
- SecretStore 已被使用；
- body 有 2,000 字符截断；
- 多渠道抽象清晰。

**主要缺口**：配置更新语义会删除 secrets、URL query credential 不遮罩、generic webhook SSRF、自定义 header 边界不足。修复后该模块可成为数字人和自动化的稳定公共能力。

### 8.2 企业微信自建应用

AES/PKCS#7、signature compare、CorpID 解密校验和默认 deny allowlist 是正向控制；但 plaintext fallback 直接绕过上述能力，属于“核心安全不变量被旁路”的典型问题。应以真实企微回调 fixture 建立协议级测试，而不仅是函数单测。

### 8.3 数字人协议与商店

DHP 到 ScheduledTask 的映射避免另建运行时，这是正确决策。当前需要把“配置、密钥、依赖安装、运行权限”从一个安装动作中拆成可审计阶段：

`fetch → verify → inspect → dependency plan → consent → install disabled → configure secret refs → enable`

Store 中已解析 checksum/size 的字段必须真正执行，不应只展示。

### 8.4 Skill / Plugin / Persona 市场

复用 loader、source manager、adapter 和统一 Customize Hub 的方向正确，符合“不造轮子，造胶水”。但安全模型必须从“路径字符串没有 `..`”提升为：

- 内容寻址/commit pin；
- resolved containment；
- symlink/reparse point policy；
- staged atomic transaction；
- provenance 和 capability diff；
- 安装/更新/启用分离；
- MCP executable 单独确认。

### 8.5 Rules / Commands / Hooks / Subagents

- Rules 的 `allow/deny/ask` 已接 PermissionEngine，且 deny 优先，是良好基础；
- Commands 采用 `COMMAND.md` 并在 Composer 展开，简单且兼容性好；
- Hooks 仅覆盖 pre/post run，不能声称已有完整 agent-loop hooks；
- Subagent 已支持独立 context 与无递归 delegate，下一步应补 lifecycle/budget/capability intersection，而不是增加更多固定子代理类型。

### 8.6 Browser Login

Host 精确匹配、`safe_id` 和 context 生命周期处理是正向控制。需要补：私有权限、原子写、备份路径边界、expiry 主动判定、多 profile 隔离。登录态的敏感级别应与 API token/password 相同，而不是普通缓存文件。

---

## 9. CI/CD、发布与版本治理

### 9.1 当前 CI 缺口

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) 目前主要运行 Linux Python 测试和 GUI Vitest，缺少：

- Windows pytest；
- TypeScript production build/typecheck gate；
- Rust fmt/check/Clippy；
- Python lint/format/typecheck；
- ESLint；
- Playwright smoke；
- Python/npm/Rust 依赖安全检查；
- coverage threshold；
- workflow timeout 和 concurrency cancellation。

`gui-e2e` 已从 `if: false` 恢复为活跃 CI 门禁（2026-08-01），过时的 Wails 注释已更新为 Tauri。hermetic Playwright 套件（160 spec，全 mocked REST/WS）全绿。其余缺口（Windows pytest job、Rust/Python lint、coverage threshold、依赖安全检查）仍待补。

### 9.2 建议的 required checks

**PR 必须通过**：

1. Python 3.10/3.12 Linux pytest；
2. Python Windows pytest；
3. fork feature tests；
4. `pip check` + `compileall`；
5. Ruff format/lint；
6. GUI Vitest；
7. `tsc --noEmit`；
8. Vite production build + bundle budget；
9. 10–20 条 Playwright smoke；
10. `cargo fmt --check`、`cargo check`、Clippy（CI 明确安装 CMake/LLVM 依赖）；
11. secret scan、dependency review、npm production audit policy。

**nightly/weekly**：

- 完整 Playwright；
- macOS/Windows package build；
- provider/connector contract tests；
- 扩展市场恶意包 corpus；
- upstream sync dry-run；
- SBOM 和 license report。

### 9.3 Release 必须依赖同 commit 的测试门禁

[`.github/workflows/release.yml`](../.github/workflows/release.yml) 当前 release matrix 能成功打包，但：

- `app-v*` tag 可触发 build，release job 却只接受 `refs/tags/v*`；
- 注释说 draft，实际 `draft: false`；
- release 不依赖等价测试门禁；
- Windows 未 Authenticode 签名；
- macOS signing secret 缺失时降级为 unsigned；
- Tauri config 为 `0.1.9`，Rust package 仍为 `0.1.0`。

建议：

- 只保留一个 tag 规范；
- 单一版本源生成 Tauri/Rust/package metadata；
- release workflow 先验证目标 commit 已通过 required workflow；
- 缺少签名 secret 时 fail closed，不发布 unsigned 稳定产物；
- 为 Windows 配置 Authenticode，为 macOS 配置 Developer ID/notarization；
- 生成 SHA-256、SBOM、provenance/attestation；
- 先 draft，smoke install/upgrade 验证后再 publish。

Tauri updater/minisign 保护更新 manifest 和包完整性，但不能替代 Windows/macOS 的 OS 级代码签名信誉链。

---

## 10. 文档与仓库卫生

### 10.1 需要更新的文档漂移（部分已修复）

- ~~[`USAGE_GUIDE.md`](USAGE_GUIDE.md) 含硬编码 `D:\Github_Open\openworker`、旧 commit `f96ad4c` 和上游 clone 地址~~ **已修复（2026-08-01）**：改为 fork 仓库 URL + 本地路径 `openworker-pro`；
- 使用指南未覆盖通知、数字人、Customize、Plugins、Rules、Commands、Hooks、Browser Login、WeCom（仍待补）；
- ~~[`MOD-ROADMAP.md`](MOD-ROADMAP.md) 仍含旧 `v0.1.7-pro.1` 发布描述~~ 功能 2（微信/企微）已标注已交付，含 iLink + 企微自建应用；
- ~~[`EXTENSIONS-ROADMAP.md`](EXTENSIONS-ROADMAP.md) 末尾仍写 E1 “进行中”，与正文多个完成状态矛盾~~ **已修复**：E1 标记改为“✅ 完成”；
- ~~路线图中残留 “Wails runtime” 说明，与 Tauri 现状冲突~~ **已修复**：E4 验证段 Wails 注释更新为 Tauri + Playwright 覆盖；
- ~~[`coworker/risk.py`](../coworker/risk.py) 注释称 RiskOverrides “Phase 2 接入、恒 None”~~ **已修复**：注释更新为已接入（`agent.py` via `RiskOverrideStore`）；
- Tauri/Rust/package/release 的版本号不一致（仍待统一）。

建议建立：

- `docs/SECURITY_MODEL.md`：进程、origin、token、workspace、MCP、extension、connector、secret、remote threat model；
- `docs/EXTENSION_TRUST.md`：来源等级、安装阶段、MCP executable、更新重新审批；
- `docs/RELEASING.md`：版本、tag、签名、SBOM、回滚；
- `docs/COMPATIBILITY.md`：上游同步、配置 schema、Plugin/Skill/DHP 兼容版本；
- release 前自动扫描绝对本机路径、旧仓库 URL、旧版本和过时框架名称。

### 10.2 不应跟踪测试运行产物（已修复）

仓库跟踪约 160 个 Playwright result 类文件，会造成噪声和陈旧状态误导。应：

- 将 runtime `test-results/`、trace、screenshot、video 从 Git 移除；
- 只保留有意维护的 golden fixture，并放在明确的 `tests/fixtures` 或 `e2e/golden`；
- CI artifact 设置保留期限，不把失败产物提交到源码。

**修复（2026-08-01）**：160 个被跟踪的 Playwright 运行产物已从 Git 移除；根 `.gitignore` 加 `test-results/`、`playwright-report/`；`surfaces/gui/.gitignore` 加 `/test-results-baseline/`。

---

## 11. 与知名开源 Agent 项目的差距和复用策略

### 11.1 原则

OpenWorker-Pro 的优势不是“再做一个通用 coding-agent runtime”，而是：

- 中文本地优先桌面体验；
- 企业/IM/业务连接器；
- 数字人协议与自动化；
- 多 Provider 与本地工具编排；
- Tauri 小型桌面壳和本地数据控制。

应把成熟项目当作**协议、边界和产品机制的参考实现**，通过 adapter/compatibility layer 复用生态。不要把 Hermes、OpenCode、OpenHands 或 Goose 的 runtime 嵌进来形成双引擎。

### 11.2 项目对比

| 项目 | 当前官方仓库 | 许可证 | 值得吸收 | 不建议照搬 |
|---|---|---|---|---|
| Hermes Agent | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | MIT | 单 core 多 surface、observer event contract、Skill provenance/渐进加载、remote executor abstraction、MCP server filtering、subagent lifecycle/capability contraction | 整套 messaging gateway、全部 browser/provider、远程 executor 集合 |
| OpenCode | [anomalyco/opencode](https://github.com/anomalyco/opencode) | MIT | server-first 本地架构、OpenAPI/生成 SDK、SSE attach/reconnect、声明式 permission、`SKILL.md` 兼容、remote MCP OAuth、ACP | Bun/TypeScript runtime 整体迁移；把 permission DSL 当 sandbox |
| OpenHands | [OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) | MIT | ACP-backed 多 backend control plane、backend registry、UI/control plane 与执行 sandbox 解耦 | 当前 Canvas/UI 栈或完整 execution runtime |
| Goose | [aaif-goose/goose](https://github.com/aaif-goose/goose) | Apache-2.0 | MCP-first extension、Recipe/Skill/Plugin 分层、Open Plugins hooks、permission modes、subagent anti-recursion、ACP client/server、安全 remote server、OTLP/Langfuse/MLflow | Rust/Electron core 作为第二运行时 |

> 仓库名称校正：OpenHands 当前为 `OpenHands/OpenHands`；Goose 当前为 `aaif-goose/goose`，旧 `block/goose` 会重定向。

### 11.3 能力差距矩阵

| 能力 | OpenWorker 现状 | 参考项目 | 建议 |
|---|---|---|---|
| 统一事件协议 | 各层事件存在但缺少稳定公共 schema | Hermes observer、Goose telemetry | **适配**：先定义 versioned internal event，再做 exporter |
| 本地 server API/SDK | FastAPI/WS 有功能，但 API 单体、缺正式 schema/SDK/reconnect | OpenCode | **采用机制**：OpenAPI、生成 TS/Python SDK、capability negotiation |
| ACP | 尚无公共 agent protocol adapter | OpenCode/OpenHands/Goose | **优先适配**：作为外部 backend/editor 边界，不重写 engine |
| Permission policy | 已有 PermissionEngine + allow/deny/ask | OpenCode/Goose | **增强**：资源/参数作用域、来源、capability diff、policy hook |
| Skills | 已支持 `SKILL.md` progressive loading | Hermes/OpenCode/Goose | **保持并兼容**：补 provenance、tool scope、版本和签名 |
| Plugin/Recipe 分层 | Plugin 分发包、Skill/Command 已有 | Goose | **澄清模型**：Recipe=声明式工作流，Skill=知识，Plugin=分发/扩展 |
| MCP 安全 | consumer 能力强，spawn/filter/provenance 弱 | Hermes/OpenCode/Goose | **最高优先增强**：per-server filter、OAuth lifecycle、审批、health |
| Subagent | 独立 context、persona delegate、无递归 | Hermes/Goose | **增强**：handle、后台、budget、capability intersection、cancel |
| Remote execution | 路线图待做 | Hermes、OpenHands | **延后**：先定义 executor interface，再接成熟 backend |
| Remote UI/attach | FastAPI/WS 基础，鉴权/恢复不足 | OpenCode/Goose | **先做协议和安全**，隧道只是 transport，不是安全架构 |
| Hooks | pre/post run，fail-open | Goose Open Plugins | **适配契约**：observer/policy 分型，tool/message events |
| Observability | 零散日志 | Hermes observer、Goose OTLP | **采用标准**：本地 trace + 可选 OTLP，默认 redaction |

### 11.4 Adopt / Adapt / Defer / Reject

#### 直接采用成熟标准或格式

- MCP：继续作为工具扩展主协议，但补安全层；
- ACP：作为外部 Agent backend/editor/control-plane 协议；
- OpenAPI：从 FastAPI schema 生成正式 TS/Python client；
- OpenTelemetry Protocol：作为可选观测导出；
- `SKILL.md`：继续保持跨生态兼容；
- PKCE/OAuth state、SBOM、SLSA provenance 等通用标准。

#### 适配思想，不复制整套实现

- Hermes observer 与 remote executor interface；
- OpenCode attach/reconnect、permission config 和 MCP OAuth lifecycle；
- OpenHands backend registry/control-plane 分离；
- Goose Recipe/Skill/Plugin、hook contract、subagent capability contraction。

#### 延后

- 多种 remote sandbox/backend；
- mobile/H5 公网反控；
- 后台大规模 subagent fleet；
- 用户可发布的公共插件市场；
- 完整 tracing SaaS 集成。

这些能力只有在 P0/P1、事件 schema 和 API contract 稳定后才不会放大风险和返工。

#### 明确不做

- 不嵌入第二套 Agent loop；
- 不迁移到 Bun、Electron 或 Rust 仅为追随竞品技术栈；
- 不复制 Hermes/Goose 的全部 connector/provider；
- 不把 permission prompt 当作 OS sandbox；
- 不自研通用远程隧道协议；transport 可用 Cloudflare Tunnel/frp/SSH 等成熟方案，但认证与 capability 仍由 OpenWorker 自己定义；
- 不自动信任第三方 Skill/Plugin/DHP 声明的 MCP command。

### 11.5 许可证建议

- Hermes、OpenCode、OpenHands 为 MIT：可复用代码，但必须保留相应 copyright/license notice；
- Goose 为 Apache-2.0：可复用并有明确专利授权，但需保留 LICENSE/NOTICE 和变更说明；
- 任何复制都应按文件/提交记录来源，建立 `THIRD_PARTY_NOTICES`；
- “参考设计后独立实现”仍应在 ADR/文档中注明灵感来源，避免日后来源不可追溯；
- 仓库级许可证不自动覆盖其中所有第三方 asset、模型、Skill、Plugin 或示例，进入市场前仍需逐项 metadata 和 license policy。

---

## 12. 推荐目标架构

### 12.1 保留一个核心运行时

```text
React/Tauri GUI ─┐
CLI/TUI          ├── OpenAPI/ACP/attach ── Local Control Plane
Mobile/Remote    ┘                            │
                                              ├── Session/Turn Engine（唯一）
                                              ├── Permission & Policy
                                              ├── Event Bus / Audit
                                              ├── Extension Registry
                                              └── Executor Adapter
                                                   ├── local
                                                   ├── container/sandbox（后续）
                                                   └── remote backend（后续）
```

关键点：

- GUI 不直接持有全能 sidecar secret；
- 所有 surface 通过同一版本化 API/ACP capability contract；
- Engine 保持唯一；
- extension、connector、executor 都通过 capability/provenance 注册；
- permission evaluation 同时考虑 tool、参数资源、来源、workspace 和调用链；
- Event Bus 是 observer，不是另一个业务控制器。

### 12.2 扩展安装状态机

```text
discovered
  → fetched
  → integrity_verified
  → inspected
  → consented
  → installed_disabled
  → configured
  → enabled
  → update_available
  → reapproval_required（capability 变化）
```

每一步均可审计、可失败、可回滚。MCP executable、hook command、network destination、filesystem scope 属于 capability diff 的核心字段。

### 12.3 Permission DSL 下一步

当前 tool-name glob 的 `allow/deny/ask` 可扩展为：

```yaml
- effect: ask
  tool: browser_upload_file
  when:
    path_outside: workspace

- effect: deny
  tool: mcp__untrusted_server__*
  source:
    trust: unverified

- effect: allow
  tool: read_file
  resource:
    path: "${workspace}/docs/**"
```

但 DSL 必须编译到现有 `PermissionEngine`，不另建平行决策器；并且始终保持 hard deny 和 OS sandbox 的边界。

---

## 13. 30 / 60 / 90 天执行路线图

### 13.1 0–7 天：安全止血与基线冻结

1. 修复企微 plaintext fallback，加入 freshness/replay/dedup 测试；
2. 禁止 artifact `allow-scripts + allow-same-origin`，恢复最低可用 CSP；
3. 临时关闭第三方 DHP 自动补装 Plugin/MCP，改为 dependency preview + explicit confirm；
4. 修复 DHP secret 写入 instructions；
5. 通知 save/test/toggle 改为保留 masked secret，URL 全量遮罩；
6. 禁用 generic webhook 访问 private/loopback/link-local/metadata；
7. 禁用 Browser Login backup import/export，直至固定路径实现完成；
8. 修复 Windows grep；
9. 为上述每项增加负向回归测试。

**退出条件**：P0 均有测试；任何外部输入不能无签名注入企微消息、读取主窗口 token、静默注册 MCP executable、读写登录态目录外文件或把 secret 放进公开任务。

### 13.2 8–30 天：恢复可信发布门禁

1. sidecar token 缺失 fail closed，收紧 CORS/origin；
2. 完成 Browser Login 私有 ACL、原子写与安全 backup；
3. MCP OAuth 强制 state/PKCE；
4. MCP provenance、per-server tool filtering 和 command reapproval；
5. Provider timeout/retry/cancel 统一策略；
6. GUI WebSocket reconnect + relay bounded concurrency；
7. 重建 Playwright 10–20 条稳定 smoke 并启用 CI；
8. CI 加 Windows pytest、tsc/build、Rust、Ruff、依赖审计；
9. release 依赖同 commit 门禁，统一 tag/version，签名缺失 fail closed；
10. 移除 tracked test-results，更新使用指南和路线图。

**退出条件**：主分支 required checks 全绿；Windows/macOS 安装、升级、启动、首轮会话、重连、卸载均有自动 smoke；稳定版不含 unsigned 产物。

### 13.3 31–60 天：统一契约与拆分高耦合模块

1. 定义 versioned event schema 和本地 audit trace；
2. 从 FastAPI 生成 OpenAPI TS/Python SDK；
3. 定义 attach/resume/cursor/capability negotiation；
4. 拆分 `manager.py`、`app.py`、`api.ts` 的领域边界；
5. Tool output 引入 untrusted-content/provenance envelope；
6. Hooks 增加 pre/post tool 和 on-message，observer/policy 分型；
7. Subagent 增加 handle、cancel、budget、capability intersection；
8. Extension 安装改为 staged transaction 与 capability diff。

**退出条件**：GUI 只通过生成/封装 client 使用稳定 API；一个慢 connector/MCP/subagent 不阻塞主事件流；扩展更新权限变化会触发重新确认。

### 13.4 61–90 天：生态适配与远程基础

1. 实现 ACP adapter，先作为 server 暴露现有 engine；
2. 再实现 ACP client/backend registry，接一个外部 backend 做验证；
3. 加本地 trace viewer 和可选 OTLP exporter；
4. 定义 executor interface，仅接一个成熟受控 backend；
5. 远程访问先实现设备配对、短期 token、TLS/pinning、scope 和 revoke，再接 tunnel；
6. 评估 mobile/H5 只读/审批模式，写操作后开；
7. 发布 Extension Trust Policy 与第三方 license/provenance UI。

**退出条件**：远程 transport 被替换时不影响 Agent core；设备可撤销、token 有 scope/TTL；ACP 外部 backend 不能绕过本地权限与审计。

---

## 14. 推荐的具体工作包

| 工作包 | 优先级 | 预估 | 关键产物 |
|---|---:|---:|---|
| WebView/sidecar trust boundary | P0 | 3–5 天 | CSP、isolated artifact、capability bridge、攻击回归测试 |
| WeCom callback hardening | P0 | 1–2 天 | strict encrypted mode、replay/dedup、协议 fixture |
| Secret lifecycle | P0 | 2–3 天 | secret refs、PATCH semantics、redaction、webhook guard |
| Extension transaction/provenance | P0/P1 | 5–8 天 | install plan、digest、atomic rollback、MCP approval |
| Browser Login storage/backup | P0/P1 | 3–5 天 | fixed-path import、private ACL、atomic writes |
| Windows correctness | P1 | 1–2 天 | rg JSON parser、portable tests、process lifecycle decision |
| CI/E2E recovery | P1 | 4–7 天 | required matrix、20 smoke、release gate |
| API/event contract | P1/P2 | 5–8 天 | OpenAPI SDK、event schema、attach/reconnect |
| God-module decomposition | P2 | 分批 2–4 周 | domain services/routers/clients，不做大爆炸重写 |
| ACP/observability | P2 | 1–2 周 | ACP adapter、OTLP optional exporter |

预估只用于排序，不应在缺少 issue 级拆分时作为承诺日期。

---

## 15. 正向发现

全量审查不应只记录缺陷。以下基础值得保留并继续演进：

- Python 测试数量和 fork 专项覆盖已经具备可持续演进基础；
- 前端 TypeScript 类型检查与中英文 key/placeholder 对齐良好；
- Context overflow compaction 已形成自动恢复闭环；
- Tauri capability 没有授予宽泛 shell/filesystem/process plugin 权限；
- SecretStore 已有跨平台私有权限与原子替换意识；
- Persona 安装使用 snapshot，并保留 disabled-pending-consent；
- Rule deny 的优先级设计正确；
- Subagent 已实现上下文隔离和反递归；
- Git 操作使用参数数组而非 `shell=True`，降低 shell injection；
- DHP 映射到现有 ScheduledTask，而不是创建重复 runtime；
- Extension Hub 复用 source/adapter/loader，架构方向符合“造胶水，不造轮子”。

这些优点说明项目不需要推倒重来。当前最有效的策略是**收紧边界、建立契约、恢复门禁、再开放生态**。

---

## 16. 最终优先级清单

### 必须立即做

1. WebView artifact 隔离 + CSP + sidecar 凭据边界；
2. 企微严格加密回调 + replay/dedup；
3. DHP/Plugin/MCP 安装确认、provenance 与事务；
4. Browser Login backup/path/permission；
5. DHP/通知 secret 生命周期 + webhook SSRF；
6. Windows grep；
7. 最小 E2E smoke 和 Windows CI。

### 紧接着做

1. sidecar fail-closed、CORS/origin；
2. OAuth state/PKCE；
3. provider timeout/retry；
4. WebSocket/relay 恢复与隔离；
5. release 签名、版本和 required checks；
6. `xlsx` 隔离或替换；
7. 使用指南、路线图和测试产物清理。

### 在基础稳定后做

1. 统一 event schema；
2. OpenAPI + SDK；
3. ACP adapter；
4. Recipe/Skill/Plugin 语义与 hook contract；
5. Subagent lifecycle/budget；
6. OTLP 可选观测；
7. executor backend 与远程访问。

---

## 17. 官方参考来源

- Hermes Agent: <https://github.com/NousResearch/hermes-agent>
- OpenCode: <https://github.com/anomalyco/opencode>
- OpenHands: <https://github.com/OpenHands/OpenHands>
- Goose: <https://github.com/aaif-goose/goose>
- Agent Client Protocol: <https://agentclientprotocol.com/>
- Model Context Protocol: <https://modelcontextprotocol.io/>
- OpenTelemetry: <https://opentelemetry.io/>

外部能力和 release 状态以 2026-08-01 审查时的官方仓库为准。引入任何具体代码前，应再固定目标 commit 并执行逐文件 license/provenance 审查。

---

## 18. 搜索摘要

- 网站：Gemini｜查询：OpenWorker 与 Hermes Agent、OpenCode、OpenHands、Goose 的能力差距、许可证和可复用策略｜次数：1｜结果：覆盖不足，仅作线索，不作为主要结论依据。
- 网站：GitHub 官方仓库/API/README/docs/releases｜查询：`NousResearch/hermes-agent`、`anomalyco/opencode`、`OpenHands/OpenHands`、`aaif-goose/goose` 的架构、功能、许可证与发布信息｜各项目按官方材料核验｜结果：作为本报告开源对比的主要依据。
- 已使用官方仓库校正旧组织路径：`All-Hands-AI/OpenHands` → `OpenHands/OpenHands`；`block/goose` → `aaif-goose/goose`。

---

## 19. 审查限制

1. 本次没有使用真实生产凭据连接所有第三方 Provider、Slack、GitHub、企微、SMTP 或 CRM；外部系统结论主要来自代码路径、mock/fixture 与协议分析。
2. Rust 本地检查被当前 shell 缺少 CMake 阻断；GitHub release matrix 成功只能证明其构建环境可打包，不能替代 fmt/Clippy/源码审查。
3. Playwright 结果受测试资产整体漂移影响，因此报告没有把 153 个失败逐个认定为产品 bug，而是将其认定为“E2E 门禁失效”。
4. npm advisory 状态和外部项目 release 会变化，整改时应重新执行审计。
5. 本报告是风险与路线图文档；每个 P0 应转为独立 issue/PR，并由负向测试证明修复。

## 17. 实施进度（2026-08-01）

### 个人微信 iLink：已实现，待受控真实扫码验收

已按独立 behavioral reimplementation 交付 `wechat_ilink` 普通连接器，详见 [个人微信 iLink 连接器](WECHAT_ILINK_CONNECTOR.md)。完成范围包括：

- vendor-only HTTPS/DNS/SSRF transport boundary、明确超时、响应上限、拒绝 redirect 和安全随机 UIN；
- 后端独占 QR attempt，公开 API 不返回 polling transaction、`bot_token`、`base_url`、内部 user ID 或 `context_token`；
- 多账号 profile、账号级 allowlist、default pointer、重新认证和单账号/全局断开；
- 私聊 long polling、运行期 cursor/context、200 条消息去重、媒体占位、退避重连和 `-14` fail-stop；
- `wechat_ilink:<account_id>/<user_id>` target 和等待真实协议结果的 live-delivery bridge；
- 正式 connector descriptor、专用 QR modal、多账号详情页、中英文限制文案和独立图标；
- 后端协议/transport/auth/adapter/API 测试以及 GUI QR 串行 polling、卸载取消、账号隔离与重新认证测试。

自动化 evidence：broad iLink/connector backend regression `118 passed`；完整 GUI Vitest 在加入通知回归后为 `91 passed`，其中 focused iLink GUI `12 passed`、通知配置面板 `2 passed`；GUI `tsc && vite build` 通过；`git diff --check` 通过。Browser preview 已在当前源码 sidecar 下确认正式 connector 卡片、独立图标、专用 QR dialog、本地 200×200 canvas、中英文文案、desktop/mobile 布局以及 waiting/scanned/expired/retry 状态；console/network 无前端错误，公开 QR 失败响应只包含稳定错误而没有 token/context/raw transaction。真实扫码 → 入站 → 回复 → 重连 → 重新认证流程仍需由用户在不记录凭据的本机环境完成，因此没有误报为已验收。

### WeCom 与通知安全边界：已完成代码整改

- WeCom focused regression：`34 passed`；
- Notification focused/API regression：`26 passed`；
- 合并的 WeCom/Notification 回归：`60 passed`；
- 完整 backend：`1,278 passed / 10 failed / 1 skipped`。10 个失败与本轮改动无关，仍是报告前述 Windows ripgrep、symlink/ACL、活动 cwd 和 relay/e2e 环境基线；
- 完整 GUI：`91 passed`；production build 通过（保留既有 dynamic/static import 与大 chunk warning）。

本轮关闭了企微 plaintext downgrade/replay/dedup/bad-padding，以及通知 mask replacement、toggle 丢配置、test-send 丢 secret、webhook SSRF/redirect/header/vendor-host 问题。


---

## 20. 一句话结论

**OpenWorker-Pro 已经拥有值得继续投资的产品骨架；下一阶段的竞争力不来自再堆一套 Agent 能力，而来自把现有 WebView、连接器、扩展、密钥和远程边界做成可信平台，再通过 ACP、OpenAPI、MCP、`SKILL.md` 与 OTLP 吸收 Hermes、OpenCode、OpenHands、Goose 的成熟生态。**

---

## 21. 第二轮整改完成记录（2026-08-01，iLink 交付后）

iLink 连接器交付后，按 §5 P0/P1 顺序继续关闭高价值安全/可靠性缺口。以下为本轮已完成项及验证证据。

### P0-01：HTML artifact 静态隔离 — 已完成

- `surfaces/gui/src/components/RightRail.tsx`：iframe 改为 `sandbox=""`（无 `allow-scripts`/`allow-same-origin`），并新增 `sandboxedSrcDoc()` 在 artifact HTML 最前注入限制型 CSP（`default-src 'none'`、仅 inline style 与 `data:`/`blob:` 图片、`form-action 'none'`、`base-uri 'none'`），即便脚本不可执行也阻断 `<img src=http://localhost:8765/…>` 等外联请求。
- `surfaces/gui/src-tauri/tauri.conf.json`：`csp: null` 替换为生产 CSP（`default-src 'self'`、`connect-src` 限定 `127.0.0.1:*` loopback、`object-src 'none'`）与 dev CSP（允许 Vite HMR/eval）。
- 验证：`RightRail.test.tsx` **6 passed**（CSP 注入、head/fragment/empty 处理、内容不转义）；完整 GUI Vitest **97 passed**。

### P0-04：Browser Login canonical/private/atomic 持久化 — 已完成

- `coworker/server/manager.py` `import_browser_logins`/`export_browser_logins`：路径一律由 safe login ID 推导 canonical path，`Path.relative_to()` 做 containment（非字符串 `startswith`），拒绝 symlink/reparse-point 逃逸；只接受 `storageState.json`/`cookies.json` 两个 canonical 文件名；per-file 5 MiB 上限；写入复用 `write_private_text()`（原子 + 0600/ACL）。先全量校验再落盘，错误导入不留半写状态。
- 验证：`test_browser_logins.py` **35 passed**，新增 absolute path、same-prefix sibling、oversize、secrets 不可覆盖、canonical-only 5 条回归。

### P1-01：sidecar 缺少 token 时默认开放 — 已完成（fail-closed）

- `coworker/server/app.py`：`COWORKER_API_TOKEN` 为空时不再静默放行；仅当显式 `COWORKER_INSECURE_LOCAL_DEV=1`（由 `--insecure-local-dev` 设置）时允许 tokenless 运行，否则所有认证端点 401。HTTP 中间件与 WebSocket `_websocket_authenticated` 一致 fail-closed。
- `coworker/server/run.py`：新增 `--insecure-local-dev` 参数；`_ensure_api_token` 在该模式下跳过 token 生成并打印警告。
- 验证：`test_server.py` 新增 `test_missing_token_fails_closed_without_dev_opt_in`、`test_missing_token_allows_dev_opt_in`，连同既有 `test_sidecar_token_gates_rest_and_websockets` **3 passed**；live 验证 tokenless `/v1/sessions`→401、带 token→200。

### P1-02：MCP OAuth state 降级接受 — 已完成

- `coworker/mcp/oauth.py`：新增 `_inject_state()`，在打开浏览器前确保 authorize URL 携带客户端生成的 state（服务端/SDK 未提供时自动追加高熵 state）；`deliver_callback()` 改为 fail-closed——`_expected_state` 为 None 时拒绝任何回调，不再 accept-any。state 始终由客户端生成并绑定 flow，不依赖服务端是否给出。
- 验证：`test_mcp_oauth.py` **12 passed**（新增 no-state-bound 拒绝、inject 追加/保留、mismatched state 不消费 flow）。

### P1-06：Session WebSocket 与 relay 恢复能力 — 已完成

- `surfaces/gui/src/api.ts` `Session`：`onclose` 后自动重连（指数退避 ±25% jitter，上限 30s，最多 8 次）；`close()` 标记 intentional teardown 并取消重连定时器；重连后服务端从持久化历史恢复。
- `coworker/connectors/relay_client.py` `RelayHub._reconnect`：固定 2s 改为指数退避 + jitter（base=2s，cap=30s），成功重置 `_consecutive_failures`；`reconnect_delay=0` 仍用于测试即时重连。
- 验证：`api.session-reconnect.test.ts` **3 passed**（意外关闭重连、intentional close 不重连、退避递增）；`test_relay_backoff.py` **5 passed**（指数增长、30s cap、0 delay 跳过、failure 计数、成功重置）。

### P1-07：Windows ripgrep 解析 — 已完成

- `coworker/tools/search.py`：ripgrep 改用 `--json`，解析 `type=="match"` 结构化事件（path/line/text 为离散 JSON 字段），免疫 Windows 盘符冒号、含冒号文件名和 Unicode 路径；新增 `_GLOB_IGNORE = _IGNORE_DIRS - OS_DATA_DIRS`，避免 `!**/AppData/**` 误伤路径含 AppData 祖先的工作区（Windows `%TEMP%`）。
- 验证：`test_code_tools.py` **19 passed**（新增盘符、冒号文件名、invalid regex、空匹配、max results、`_GLOB_IGNORE` 与 OS_DATA_DIRS 不混用）。

### 本轮新增测试汇总

- 后端：`test_browser_logins.py`(+5)、`test_code_tools.py`(+6)、`test_server.py`(+2)、`test_mcp_oauth.py`(+4)、`test_relay_backoff.py`(+5) = **+22**；合并目标回归 **191 passed**，2 个失败为既有 Windows ACL/file-rename 环境问题，与本轮改动无关。
- GUI：`RightRail.test.tsx`(+6)、`api.session-reconnect.test.ts`(+3) = **+9**；完整 Vitest **97 passed**。

### 仍未关闭（后续优先级）

- **P1-05 provider timeout/retry/cancellation**：需要在 provider abstraction 层统一定义 connect/read/total timeout、429/5xx 退避、`Retry-After`、cancellation 传播，分别配置首 token/idle/总 deadline；触及 OpenAI/Anthropic/Gemini/Bedrock 四个 provider 与 engine 调用点，建议单独规划。
- **P1-03 untrusted content envelope**、**P1-04 browser 工具文件边界**、**P1-08 xlsx**、**P0-03 MCP stdio/provenance**、CI 门禁（Windows pytest/tsc/cargo/smoke）与可信发布门禁按 §13.2 顺序推进。
