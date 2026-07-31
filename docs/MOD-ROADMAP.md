# OpenWorker-Pro 魔改路线图

基于 `kael-odin/openworker`（中文增强 fork）魔改，功能需求来源：Halo（`openkursar/hello-halo`）+ 数字人协议（`openkursar/digital-human-protocol`）。

> 核心心态：Halo 是**功能需求来源**，不是代码来源。Halo 是 Electron+TS，openworker 是 Python 后端 + Tauri/React 前端，栈不兼容，几乎全部重写。可借鉴的是产品形态、功能边界、UI 交互模式、协议规范。

## 优先级（用户确认）

1. 通知渠道层（钉钉/飞书/企微/webhook）
2. 企业微信/微信连接器
3. 数字人商店 + 可配置面板 UI（**重点，要无缝导入 DHP 仓库**）
4. 远程访问（remote tunnel）

> 原排序 1-2-4-3，即 4（数字人）提到 3 之前。下方按此顺序展开。

---

## 架构对应关系（Halo ↔ openworker）

| Halo (Electron/TS) | openworker (Python+Tauri) | 关系 |
|---|---|---|
| `apps/manager/{store,service,types}.ts` | `coworker/personas/registry.py` + `automation/store.py` | 同构：persona 生命周期 + 调度持久化 |
| `apps/spec` (AppSpec) | `personas/manifest.py` (PersonaManifest) + `automation/models.py` | **需扩展**：对齐 DHP spec.yaml 字段 |
| `apps/runtime/` | `coworker/automation/scheduler.py` + `coworker/engine.py` | 调度执行器已有 |
| `notify-channels/` | ❌ 无 → **新建** `coworker/connectors/notify/` | 功能 1 |
| `apps/runtime/im-channels/` | `coworker/connectors/`（无 IM 入口）→ **新建** | 功能 2 |
| `remote/{tunnel,issuer-client}.ts` | ❌ 无 → **新建** | 功能 4 |
| renderer `store/` + `apps/` 组件 | `surfaces/gui/src/components/PersonasTab` 等 | **扩展**：配置面板 UI |
| Electron IPC (`ipc/`) | Tauri `invoke`（`surfaces/gui/src/tauri.ts` 集中桥） | 调用模式不同，照 tauri.ts 模式 |

## DHP 协议关键事实（功能 3 的基石）

- 仓库 `openkursar/digital-human-protocol`，CC0 协议规范 + MIT 示例。
- 数字人定义在 `spec.yaml`：`spec_version/name/version/author/type/icon/system_prompt/requires.mcps/subscriptions/config_schema/output.notify/store/memory_schema`。
- **与 openworker `personas/*.md` 同构**：都是 frontmatter(YAML) + system_prompt。字段可直接映射。
- 注册表 `index.json`：每个数字人有 `slug/name/version/path/checksum/category/tags/i18n/min_app_version`。
- `packages/digital-humans/` 下 20+ 真实数字人（ai-daily-news、bilibili-comment-replier、boss-job-monitor…）。
- `output.notify` 字段支持 system/email/WeCom/DingTalk/Feishu/webhook —— **依赖功能 1 的通知渠道层**。

---

## 功能 1：通知渠道层

**目标**：任务跑完 → 推钉钉/飞书/企微/webhook。补齐 openworker "运行结果通知"短板。

**后端**（`coworker/connectors/notify/`，新建）：
- `channels/`：`dingtalk.py`、`feishu.py`、`wecom_webhook.py`、`generic_webhook.py`、`email.py`（email 已有 senders 可复用）。
- 每个渠道一个 `send(title, body, config) -> Result` 函数，复用现有 `httpx` 体系。
- `router.py`：按数字人 `output.notify` 配置分发到多渠道。支持 `notification_level: all|important|none`（对齐 Halo `userOverrides.notificationLevel`）。
- 接入 `automation/scheduler.py`：run 结束后调 router 推送。
- 配置存储：webhook URL/token 存 `secrets.py`（不落明文），对齐 fork 的密钥管理。

**前端**（`surfaces/gui/src/components/`）：
- `NotifyChannelsSection.tsx`（借鉴 Halo 同名组件）：配置每个渠道的 webhook/secret，测试发送按钮。
- 走 `tauri.ts` invoke 模式，后端暴露 `notify_test_send` 命令。

**范围控制**：本期只做 webhook 类（钉钉/飞书/企微群机器人都是 webhook），不做企微应用 API（那是功能 2）。

**预计工程量**：后端 1-2 天，前端 1 天。

---

## 功能 2：企业微信/微信连接器

**目标**：从微信/企微触发 agent、接收通知、双向对话。中文用户场景刚需。

**合规红线**：Halo 的 `weixin-ilink.provider.ts` / `ilink-api.ts` 是逆向微信个人版协议，**法律风险高，不移植**。只做合规路径：
- **企微群机器人 webhook**（已在功能 1 覆盖，此处复用）。
- **企微自建应用 API**（需企业 corpid/secret，正规 OAuth，合规）：收消息（回调）+ 发消息 + 通讯录。
- 个人微信：明确不支持，README 注明。

**后端**（`coworker/connectors/wecom_app/`，新建）：
- `provider.py`：企微应用 API 封装（access_token 管理、消息加解密、回调校验）。
- 接入 `connectors/gateway.py` 作为 inbound listener（参考现有 slack/github relay 模式）。
- 接入 `inbox_routing.py`：企微消息 → agent 会话 → 回复到企微。
- 复用功能 1 的 notify router 做出站通知。

**前端**：`connectors/wecom/` 配置面板（corpId/secret/agentId/回调 URL 校验）。

**风险**：企微回调需要公网可达 URL —— 与功能 4（远程访问）有依赖耦合。可先用 ngrok/cloudflare tunnel 手动验证，功能 4 落地后正式集成。

**预计工程量**：3-4 天（含加解密调试）。

---

## 功能 3：数字人商店 + 可配置面板 UI（重点）

**目标**：在 openworker 里复刻 Halo 的数字人商店体验，**无缝导入 DHP 仓库的 20+ 数字人**，每个数字人有独立可配置面板。

这是用户最眼馋的部分，分三层。

### 3a. spec.yaml 解析层（后端，`coworker/personas/`）

**扩展 `PersonaManifest`** 支持 DHP spec.yaml 完整字段：
- 现有 `personas/builtin/*.md` 的 frontmatter 已覆盖：`id/name/icon/tagline/tools/messaging/connectors/recommended_models/default_permission_mode/description` + body system_prompt。
- 新增字段映射：`spec_version`、`version`、`author`、`type`(automation/skill/mcp/extension)、`requires.mcps`、`subscriptions`(schedule/file/webhook)、`config_schema`(用户配置项)、`output.notify`(绑功能 1)、`memory_schema`、`store`(slug/category/tags/locale)。
- **双向兼容**：能读 DHP spec.yaml，也能读现有 .md persona。统一成内部 `PersonaManifest` 模型。
- `subscriptions` 字段 → 生成 `automation/models.ScheduledTask`（cron 触发）。

### 3b. 商店注册表对接（后端，`coworker/personas/store.py` 新建）

- `registry_client.py`：fetch DHP `index.json`，缓存到本地（带 ETag/校验）。
- `installer.py`：按 `index.json.path` 拉取数字人包（zip 或目录），校验 `checksum`(sha256)，解包到 `~/.openworker/personas/installed/<slug>/`。
- 更新检查：对比本地安装版本 vs index 版本，提示升级。
- **无缝导入**：用户在商店点"安装" → 自动拉包校验落盘 → 注册到 `registry.py` → 可立即使用。这是"完美复刻"的核心。

### 3c. 配置面板 UI（前端，`surfaces/gui/src/components/persona-store/`，新建）

借鉴 Halo 的 `apps/` 组件矩阵，但用 openworker 的 Tauri+React+Tailwind 栈重写：

| Halo 组件 | openworker 对应 | 作用 |
|---|---|---|
| `StoreView`+`StoreGrid`+`StoreCard` | `StoreView.tsx` | 商店首页：浏览 index.json 数字人 |
| `StoreDetail`+`StoreDocumentation` | `StoreDetail.tsx` | 数字人详情：spec/文档/截图 |
| `StoreInstallDialog` | `InstallDialog.tsx` | 安装确认 + config_schema 表单 |
| `AppConfigPanel` | `ConfigPanel.tsx` | **已安装数字人的配置面板**（核心） |
| `AppNotifyChannelsSection` | 复用功能 1 的 `NotifyChannelsSection` | 绑定该数字人的 output.notify |
| `SchedulePicker`+`schedule-utils` | `SchedulePicker.tsx` | 配置 subscriptions 触发频率 |
| `SystemPromptEditor` | `SystemPromptEditor.tsx` | 查看/微调 system_prompt |
| `AppModelSelector` | 复用现有 `ModelChecklist` | 选模型 |
| `AppList`+`AppListItem` | 扩展现有 `PersonasTab` | 已安装数字人列表 |
| `FileImportZone`+`zip-import-utils` | `ImportZone.tsx` | 本地导入数字人包（不经过商店） |
| `ShareToStoreDialog` | 暂不做（需自建注册表） | 分享数字人 |

**数字人运行态 UI**：Halo 有 `ActivityEntryCard`/`ActivityThread`/`EscalationCard` 展示数字人执行历史和人工介入。openworker 可复用现有 `InboxView`/`ApprovalCard` 体系（数字人 escalations 走 inbox）。

**与现有 UI 融合**：openworker 已有 `PersonasTab`/`PersonaHero`/`AutomationQuickquick`。商店是 `PersonasTab` 之上的一个 Tab/视图，不是替换。

**依赖**：3a/3b 是后端基础；3c 前端依赖功能 1（notify 配置面板复用）。

**预计工程量**：3a 2-3 天，3b 2 天，3c 4-5 天。是四项里最大的。

---

## 功能 4：远程访问（remote tunnel）

**目标**：手机/H5 反控桌面 agent，查看数字人进度。

**不照搬 Halo**：Halo 的 `remote/{tunnel,issuer-client}.ts` 是自研隧道，openworker 用成熟方案：
- 复用 `coworker/server/`（FastAPI）已暴露的 HTTP/WS，加一层鉴权。
- 隧道方案二选一（待用户决策）：
  - **Cloudflare Tunnel**（推荐，免费、稳定、无需公网 IP）：桌面起 cloudflared，把本地 8765 暴露到 `*.trycloudflare.com` 或自有域名。
  - **frp**（自建 frps，可控性高但需服务器）。
- 鉴权： issuer-client 模式（短期 token，扫码绑定设备），参考 Halo `issuer-client.ts` 的设备配对思路但重写。
- 前端：移动端 H5 复用 GUI 的 React 组件（Tauri 的 webview 和移动浏览器同栈，已有 `vite.config.mobile.ts` 雏形）。

**依赖**：功能 2 企微回调需要公网 URL，功能 4 落地后可正式用。但功能 4 不阻塞功能 2（可先 ngrok 验证）。

**预计工程量**：3-4 天（含移动端 H5 适配）。

---

## 依赖关系与执行顺序

```
功能1 (notify) ──┬─→ 功能3c (配置面板复用 notify section)
                 └─→ 功能2 (企微出站通知复用 notify router)
功能2 (wecom) ────→ 功能4 (企微回调需公网 URL，但可先 ngrok)
功能3a (spec解析) → 功能3b (store) → 功能3c (UI)
```

## 建议执行批次（按实际执行顺序）

1. ✅ **批次 A**（commit `32cb35f`，2026-07-31）：功能 1（notify）— 5 渠道（钉钉/飞书/企微群机器人/generic webhook/email SMTP）+ router 按 level 分发 + SecretStore 存密钥 + scheduler 钩子 + `/v1/notify/*` 端点 + `NotifyChannelsSection` 面板。19 测试全绿，端到端实测通过。
2. ✅ **批次 B**（commit `fb27c06`，2026-07-31）：功能 3a + 3b（spec 解析 + store）— `coworker/digital_human/` 包（spec.py 纯解析器全字段覆盖 / store.py DhpRegistry / instances.py InstanceStore / installer.py 桥接 ScheduledTask）。映射：DHP spec → ScheduledTask 工厂，不另起运行时。43 测试全绿（含 34 真实 spec 回归），curl 实测通过。
3. ✅ **批次 C**（commit `6d411ed`，2026-07-31）：功能 3c（配置面板 UI）— `DigitalHumansSection` 商店面板（分类/搜索/动态配置表单/安装/实例管理）+ `bot` 图标 + Settings「数字人」tab + i18n。tsc 0 错误，端到端浏览器验证通过（ai-daily-news 安装/卸载全链路）。
4. ✅ **批次 D**（commit `bb5e204`，2026-07-31）：功能 2（企微自建应用）— `coworker/connectors/wecom_app/` 包（crypto.py AES-256-CBC 加解密 + SHA1 签名 + PKCS#7 / provider.py WeComAppClient access_token 缓存 + 消息解析 / adapter.py handle_callback GET 验证 + POST 接收）。接入 BasePlatformAdapter 契约，零改 gateway/_dispatch_inbound。回调走 tokenless webhook `/v1/connectors/wecom/callback`（GET echostr + POST 解密）。descriptor 驱动连接表单（corpid/secret/agent_id/token/encoding_aes_key/allowed_users 五件套 + 白名单），密钥字段走 SecretStore。`WecomDetail` 详情页（回调 URL + 加密模式 + 工具 + 白名单）+ WecomLogo + i18n。pycryptodome 加入 messaging extras。27 测试全绿，tsc 0 错误，端到端浏览器验证通过（列表 + 连接表单全字段）。
5. ⏳ **批次 E**：功能 4（远程访问）— 收尾，移动端。

这样把"眼馋的 3"拆成 B+C 两步，C 不会卡在没基础；功能 1 先行让 3c 的 notify 配置有东西可复用。

## 发版策略（待第一个功能落地后定）

- pro 仓库版本号方案：`v0.1.7-pro.1`（基线上游 + pro 增量序号），与中文 fork 的 `v0.1.7` 区分。
- release.yml 发布物命名是否改 `OpenWorker-Pro-*`：发版前决策。
- minisign 签名密钥：pro 仓库需独立配 GitHub secrets（不能复用中文 fork 的）。

## 参考文件位置

- Halo 源码：`D:\Github_Open\hello-halo`
  - 数字人管理：`src/main/apps/manager/`、`src/main/apps/runtime/`
  - 商店 UI：`src/renderer/components/store/`、`src/renderer/components/apps/`
  - 通知渠道：`src/main/services/notify-channels/`
  - IM：`src/main/apps/runtime/im-channels/`
  - 远程：`src/main/services/remote/`
- DHP 协议：`https://github.com/openkursar/digital-human-protocol`
  - 规范：`spec/app-spec.md`、`spec/package-format.md`、`spec/registry-protocol.md`
  - 注册表：`index.json`
  - 数字人包：`packages/digital-humans/`
- openworker pro：`D:\Github_Open\openworker-pro`
  - persona：`coworker/personas/`（registry.py/manifest.py/loading.py/builtin/）
  - 调度：`coworker/automation/`（scheduler.py/store.py/models.py）
  - 连接器：`coworker/connectors/`
  - GUI：`surfaces/gui/src/`（tauri.ts/components/PersonasTab/AutomationQuickstart）
